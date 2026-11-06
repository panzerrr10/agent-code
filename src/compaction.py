"""上下文压缩。

## 问题

单次运行最多 50 步，**每一步的工具结果都会累积进 messages**，中间没有任何压缩。
量化一下会怎么爆：

- 工具 schema 每步重发，约 1800 tokens × 50 步 ≈ 90,000
- 一次 ``read_file``（500 行）约 20K 字符 ≈ 6,700 tokens，读十来个文件就 67,000

所以长任务不是慢慢变慢，而是**到某个点突然 400 或质量崩掉**。

## 三条不能碰的红线

1. ``assistant.tool_calls`` 与 ``role:tool`` 消息必须**配对**——悬空就 400
2. 压缩后的 messages 会存进 ``pending_state`` 并被续跑复用，必须仍然自洽
3. 第一条 user 消息（原始任务）必须保留——丢了模型就不知道自己在干嘛

**由此推出的核心原则：Tier 1 只改 ``content``，不动消息结构。**

## 两层策略

- **Tier 1 工具结果渐进剪枝**：从最旧开始把工具结果换成一行占位，保留最近 N 条全文。
  零成本、不改结构、永远安全。
- **Tier 2 成段折叠**：还超预算时，把完整的「assistant + 它的 tool 回复」整段移除，
  换成一条**抽取式**摘要（工具名 + 参数 + 结果首行）。**不额外调 LLM** ——
  确定性、可测试、不会因为摘要调用失败而卡住任务。

Tier 3（LLM 摘要）按用户要求**不做**。
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# 沿用项目里已有的估算口径（见 agent.py 的跨轮历史裁剪），保持一致
CHAR_PER_TOKEN = 3

DEFAULT_BUDGET_TOKENS = 32000
KEEP_RECENT_TOOL_RESULTS = 8
# Tier 2 至少要给最近这么多条「段」留全文
KEEP_RECENT_SEGMENTS = 6
# 单条工具结果保留的开头长度（剪枝后）
HEAD_CHARS = 160

# 「已压缩」哨兵。
#
# 原来这里用 ``content.startswith("[")`` 当判据 —— **这是个真 bug**：
# 以 ``[`` 开头的工具输出极其常见（``run_python`` 打印列表 → ``[1, 2]``；
# MCP 返回 JSON 数组）。实测一条 30 万字符、以 ``[`` 开头的结果会让压缩
# **完全失效**（after_tokens 仍是指望的 3 倍），而且 pairing 自检还是过的，
# 于是带着超预算的请求发出去 → 400 → 重试 3 次 → 整轮失败。
#
# 改成「一个现实中不可能出现的完整前缀」：既不用往 messages 里塞额外字段
# （那会随请求发给 API），也不需要序列化前剥离。
COMPACTED_PREFIX = "[⛔上下文压缩占位]"

# 压缩的**低水位**：一旦触发压缩，就一路压到 budget × LOW_WATER，而不是
# 「刚好压进预算」。
#
# 为什么要这样：实测长任务里，压到刚好卡线意味着**下一步立刻又超预算**，
# 于是压缩每一步都触发、每一步都去改写历史中段 —— 直接把 prompt 前缀缓存
# 打碎（实测聚合命中率只有 61%，且每秒都在锯齿式暴跌）。
# 压到低水位后，压缩频率下降，历史被改写的次数随之下降。
LOW_WATER_RATIO = 0.6

# user / 附件内容的单条上限。超了就「保头保尾」截断 —— 头部通常是需求描述，
# 尾部通常是日志的最后一段（最可能包含错误信息）。
MAX_USER_CONTENT_CHARS = 24000
USER_HEAD_CHARS = 16000
USER_TAIL_CHARS = 6000


def estimate_tokens(messages: list) -> int:
    """粗估 messages 的 token 数。

    按字符数除以 3 —— 中英混排的经验值，偏保守（宁可早压缩）。
    """
    total = 0
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            # 多模态：只算文本块，图片按固定值记
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        total += len(part.get("text") or "")
                    elif part.get("type") == "image_url":
                        total += 1200
        tc = m.get("tool_calls")
        if tc:
            total += len(json.dumps(tc, ensure_ascii=False))
    return total // CHAR_PER_TOKEN


def check_pairing(messages: list) -> list:
    """检查 tool_call / tool 消息配对。返回问题列表（空表示没问题）。

    **这是压缩的安全绳**：只要它不为空，就说明压缩破坏了结构，
    发给 LLM 一定 400。生产代码里压缩后会校验一次，测试里也断言。
    """
    problems = []
    for i, m in enumerate(messages or []):
        if not isinstance(m, dict):
            continue
        if m.get("role") == "assistant" and m.get("tool_calls"):
            ids = [tc.get("id") for tc in m["tool_calls"] if isinstance(tc, dict)]
            # 找到紧随其后的 tool 消息
            replied = set()
            for nxt in messages[i + 1:]:
                if not isinstance(nxt, dict):
                    continue
                if nxt.get("role") == "tool":
                    replied.add(nxt.get("tool_call_id"))
                elif nxt.get("role") == "assistant":
                    break
            missing = [x for x in ids if x not in replied]
            if missing:
                problems.append(f"assistant#{i} 的 tool_call {missing} 没有对应 tool 消息")
        if m.get("role") == "tool":
            cid = m.get("tool_call_id")
            if not any(
                isinstance(p, dict) and p.get("role") == "assistant" and p.get("tool_calls")
                and any(isinstance(tc, dict) and tc.get("id") == cid for tc in p["tool_calls"])
                for p in messages[:i]
            ):
                problems.append(f"tool#{i} 的 tool_call_id={cid} 找不到对应的 assistant")
    return problems


def _placeholder(name: str, content: str, dropped: int) -> str:
    """把一个工具结果换成一行占位（**保留消息本身**，只改 content）。"""
    head = " ".join(str(content).split())[:HEAD_CHARS]
    return (
        f"{COMPACTED_PREFIX}{name} 的输出已省略：原 {len(content)} 字符，"
        f"本次上下文压缩省略了 {dropped} 字符。开头：{head}…]"
    )


def is_compacted(content) -> bool:
    """这条内容是否已经是**压缩占位**（别拿内容首字符猜，用完整哨兵前缀）。"""
    return isinstance(content, str) and content.startswith(COMPACTED_PREFIX)


def is_truncated(content) -> bool:
    """这条内容是否已经被「保头保尾」截断过。

    与 ``is_compacted`` 的区别：占位是**整个替换**（前缀在开头），
    而 user 内容是被截断（标记在**中间**，头尾都保留）。
    两者不能共用一个判据。
    """
    return isinstance(content, str) and COMPACTED_PREFIX in content


def _tool_name_for(messages: list, idx: int) -> str:
    """反查某条 tool 消息对应哪个工具（从前面最近的 assistant.tool_calls 找）。"""
    cid = messages[idx].get("tool_call_id")
    for m in reversed(messages[:idx]):
        if isinstance(m, dict) and m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                if isinstance(tc, dict) and tc.get("id") == cid:
                    fn = (tc.get("function") or {}).get("name")
                    return fn or "工具"
    return "工具"


def prune_tool_results(messages: list, keep_recent: int = KEEP_RECENT_TOOL_RESULTS) -> int:
    """Tier 1：把最旧的工具结果换成占位，保留最近 ``keep_recent`` 条全文。

    只改 content，不删消息 —— 配对结构完全不受影响。
    返回省下的字符数。
    """
    tool_idx = [i for i, m in enumerate(messages) if isinstance(m, dict)
                and m.get("role") == "tool"]
    # 已经剪过的不用再剪
    victims = tool_idx[:-keep_recent] if keep_recent else tool_idx
    saved = 0
    for i in victims:
        content = messages[i].get("content")
        if not isinstance(content, str):
            continue
        if is_compacted(content):            # 已经剪过了
            continue
        # 已经很短的没必要剪（省不了多少，还丢信息）
        if len(content) <= HEAD_CHARS * 2:
            continue
        name = _tool_name_for(messages, i)
        new = _placeholder(name, content, len(content) - HEAD_CHARS)
        saved += len(content) - len(new)
        messages[i]["content"] = new
    return saved


def _segments(messages: list) -> list:
    """把 messages 切成「段」。

    一段 = 一条 assistant（可能带 tool_calls）+ 紧随其后的 tool 回复。
    开头连续的 system / 第一条 user 单独成段并标记为不可折叠。
    """
    segs = []
    i = 0
    n = len(messages)
    while i < n:
        m = messages[i]
        if not isinstance(m, dict):
            i += 1
            continue
        role = m.get("role")
        if role == "system":
            j = i
            while j < n and isinstance(messages[j], dict) and messages[j].get("role") == "system":
                j += 1
            segs.append({"start": i, "end": j, "kind": "system"})
            i = j
            continue
        if role == "user":
            segs.append({"start": i, "end": i + 1, "kind": "user"})
            i += 1
            continue
        # assistant（可带 tool 回复）
        j = i + 1
        while j < n and isinstance(messages[j], dict) and messages[j].get("role") == "tool":
            j += 1
        segs.append({"start": i, "end": j, "kind": "assistant"})
        i = j
    return segs


def _digest(seg: dict, messages: list) -> str:
    """把一段压成一行抽取式摘要（不调 LLM）。"""
    parts = []
    for k in range(seg["start"], seg["end"]):
        m = messages[k]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                fn = (tc.get("function") or {}).get("name", "?")
                raw = (tc.get("function") or {}).get("arguments") or ""
                try:
                    args = json.loads(raw) if isinstance(raw, str) else (raw or {})
                    keys = ", ".join(f"{k}={str(v)[:40]}" for k, v in list(args.items())[:2])
                except Exception:                       # noqa: BLE001
                    keys = str(raw)[:60]
                parts.append(f"{fn}({keys})")
        elif m.get("role") == "tool":
            c = m.get("content")
            if isinstance(c, str):
                parts.append("→ " + " ".join(c.split())[:80])
    text = "；".join(p for p in parts if p)
    return text[:400]


def fold_old_segments(messages: list, keep_recent: int = KEEP_RECENT_SEGMENTS) -> int:
    """Tier 2：把最旧的整段折叠掉，换成一条摘要。

    **只折叠完整的段**（assistant + 它的全部 tool 回复一起移除）—— 这样
    tool_call 与其回复要么都在、要么都不在，配对永远成立。
    第一条 user（原始任务）永不折叠。
    返回折叠掉的段数。
    """
    segs = _segments(messages)
    if len(segs) <= keep_recent + 1:
        return 0

    # 找出「第一个 user（原始任务）」——它和它之前的所有段都不许动
    first_user = next((i for i, sg in enumerate(segs) if sg["kind"] == "user"), None)
    if first_user is None:
        return 0
    protect_until = first_user + 1                     # system + 原任务 都保留

    # 只折「真正干过活」的段（带 tool_calls）。纯文本的 assistant 回复很便宜，
    # 折了反而丢结论，没必要。
    candidates = [
        i for i, sg in enumerate(segs)
        if i >= protect_until and sg["kind"] == "assistant"
        and any(isinstance(messages[k], dict) and messages[k].get("tool_calls")
                for k in range(sg["start"], sg["end"]))
    ]
    # 保留最近的若干段全文
    foldable = candidates[:-keep_recent] if keep_recent else candidates
    if not foldable:
        return 0

    digests = []
    remove_ranges = []
    for i in foldable:
        sg = segs[i]
        # 逐段累加试算：只要「删掉它（连同已接受的那些）」会留下孤儿 tool 消息，
        # 就跳过这一段。宁可少折一段，也不能把配对搞坏 —— 配对被破坏的后果是
        # agent 直接中止整轮任务（见 agent.py 的 pairing_ok 分支）。
        trial = remove_ranges + [(sg["start"], sg["end"])]
        if _orphan_risk(messages, trial):
            logger.warning("跳过折叠：会破坏 tool_call 配对（段 %s）", i)
            continue
        d = _digest(sg, messages)
        if d:
            digests.append(d)
        remove_ranges.append((sg["start"], sg["end"]))

    if not remove_ranges:
        return 0

    if not remove_ranges:
        return 0

    # 从后往前删，下标才不会错位
    for start, end in sorted(remove_ranges, reverse=True):
        del messages[start:end]

    if digests:
        note = {
            "role": "assistant",
            "content": (
                f"[上下文压缩：前面 {len(remove_ranges)} 个步骤的详细过程已折叠。"
                f"这些步骤做过：{'；'.join(digests[:8])}"
                + ("…" if len(digests) > 8 else "")
                + "。如需重新查看某个文件或命令结果，请重新调用对应工具。]"
            ),
        }
        # 插到「第一个 user 之后」，保证原始任务仍在最前面
        insert_at = 0
        for i, m in enumerate(messages):
            if isinstance(m, dict) and m.get("role") == "user":
                insert_at = i + 1
                break
        messages.insert(insert_at, note)

    return len(remove_ranges)


def emergency_prune(messages: list, budget_tokens: int, overhead_tokens: int = 0) -> int:
    """最后的兜底：连「最近的」工具结果也剪，从最旧开始，直到进入预算。

    为什么需要它：Tier 1 会保护最近 N 条，但**那一条本身就可能是巨型的**
    （读了个几十万字符的文件），光它一个就能撑爆预算。
    这时候只能连它一起压 —— 总比发一个必然 400 的请求好。
    """
    tool_idx = [i for i, m in enumerate(messages)
                if isinstance(m, dict) and m.get("role") == "tool"]
    saved = 0
    for i in tool_idx:
        if estimate_tokens(messages) + overhead_tokens <= budget_tokens:
            break
        content = messages[i].get("content")
        if is_compacted(content):
            continue
        name = _tool_name_for(messages, i)
        # 兜底时留得更少，因为它已经是最新的了，只能牺牲
        head = " ".join(content.split())[:HEAD_CHARS // 2]
        new = (
            f"{COMPACTED_PREFIX}{name} 的输出因上下文压缩被省略"
            f"（原 {len(content)} 字符）。开头：{head}…]"
        )
        saved += len(content) - len(new)
        messages[i]["content"] = new
    return saved


def _truncate_user_text(text: str) -> str:
    """把一条超长的 user 文本压成「头 + 尾」。"""
    if len(text) <= MAX_USER_CONTENT_CHARS:
        return text
    head = text[:USER_HEAD_CHARS]
    tail = text[-USER_TAIL_CHARS:]
    dropped = len(text) - len(head) - len(tail)
    return (
        f"{head}\n\n{COMPACTED_PREFIX}中间 {dropped} 字符因上下文压缩被省略。"
        f"如需完整内容请把内容存成文件后用 read_file 分段读。]\n\n{tail}"
    )


def prune_user_content(messages: list) -> int:
    """Tier 4：截断超长的 user 消息（含多模态的文本块）。

    **为什么必须有这一层**：Tier 1~3 全都只碰 ``role == "tool"`` 和 assistant 段，
    user / system 消息根本不可压。实测 ``[system, user(20 万字符)]`` 走完
    compact() 后 after_tokens 仍是 69,367（预算 32,000）—— 而 agent 只看
    pairing_ok，于是这个必然 400 的请求照样发出去。

    这恰好命中开发者最高频的动作：**把一大段日志 / 堆栈 / 代码粘进输入框**。
    也覆盖附件预览（每个附件内联 6000 字符、附件数无上限）。

    返回省下的字符数。第一条 user（原始任务）也会被截断 —— 保头保尾已经
    留下了需求描述，比发一个必然失败的请求好。
    """
    saved = 0
    for m in messages:
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str):
            new = _truncate_user_text(content)
            if new != content:
                saved += len(content) - len(new)
                m["content"] = new
        elif isinstance(content, list):
            # 多模态：content 是 [{"type":"text",...}, {"type":"image_url",...}]
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "text":
                    continue
                text = part.get("text")
                if not isinstance(text, str):
                    continue
                new = _truncate_user_text(text)
                if new != text:
                    saved += len(text) - len(new)
                    part["text"] = new
    return saved


def repair_pairing(messages: list) -> int:
    """就地修复 tool_call / tool 配对，返回修掉的问题数。

    做两件事：
    1. 删掉「找不到对应 assistant」的孤儿 ``tool`` 消息
    2. 把「没有得到任何回复」的 ``tool_calls`` 从 assistant 消息上摘掉
       （一个都不剩时连 ``tool_calls`` 字段一起删）

    为什么需要它：修好 ``fold_old_segments`` 之后配对理论上不会再坏，
    但历史 pending_state、旧数据、以及未来新增的压缩策略都可能造出坏结构。
    原来遇到这种情况是**直接中止整轮任务**（已跑步骤全废）——
    能自愈的就别让用户重新来过。
    """
    if not messages:
        return 0

    fixed = 0
    valid_ids = set()
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                if isinstance(tc, dict) and tc.get("id"):
                    valid_ids.add(tc["id"])

    # 1. 孤儿 tool 消息
    kept = []
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "tool":
            cid = m.get("tool_call_id")
            if cid and cid not in valid_ids:
                fixed += 1
                continue
        kept.append(m)
    if fixed:
        messages[:] = kept

    # 2. 没有回复的 tool_calls
    replied = {
        m.get("tool_call_id")
        for m in messages
        if isinstance(m, dict) and m.get("role") == "tool"
    }
    for m in messages:
        if not isinstance(m, dict) or m.get("role") != "assistant" or not m.get("tool_calls"):
            continue
        alive = [tc for tc in m["tool_calls"]
                 if isinstance(tc, dict) and tc.get("id") in replied]
        if len(alive) == len(m["tool_calls"]):
            continue
        fixed += len(m["tool_calls"]) - len(alive)
        if alive:
            m["tool_calls"] = alive
        else:
            m.pop("tool_calls", None)
    return fixed


def _orphan_risk(messages: list, ranges: list[tuple[int, int]]) -> bool:
    """删掉这些区间后，会不会留下「找不到 assistant 的 tool 消息」。

    这是 ``fold_old_segments`` 的第二道保险。第一道是 ``_segments`` 只会把
    紧跟 assistant 的连续 tool 归入同段 —— 但如果 messages 里出现了
    ``assistant(tool_calls) → user → tool`` 这种交错（旧版本 agent.py 在
    「工具连续失败 2 次」时就会插一条 user 警告进去），那条 tool 会自成一段，
    于是删掉 assistant 段就把它的回复变成孤儿 → 配对破坏 → 整轮任务中止。
    """
    removed = set()
    for start, end in ranges:
        removed.update(range(start, end))
    if not removed:
        return False

    removed_ids = set()
    for i in removed:
        m = messages[i]
        if isinstance(m, dict) and m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                if isinstance(tc, dict):
                    removed_ids.add(tc.get("id"))
    if not removed_ids:
        return False

    for i, m in enumerate(messages):
        if i in removed or not isinstance(m, dict):
            continue
        if m.get("role") == "tool" and m.get("tool_call_id") in removed_ids:
            return True
    return False


def compact(messages: list, budget_tokens: int = DEFAULT_BUDGET_TOKENS,
            overhead_tokens: int = 0, keep_recent: int = KEEP_RECENT_TOOL_RESULTS,
            fold: bool = True, low_water_ratio: float = LOW_WATER_RATIO) -> dict:
    """就地压缩。返回统计信息（供 SSE 事件与测试使用）。

    ``overhead_tokens``：不在这份 messages 里、但每次请求都会发的东西
    （主要是工具 schema）。不算进去的话预算会失真。

    ``low_water_ratio``：**触发线是 budget，目标是 budget × 这个比例**。
    压低水位是为了减少压缩频率 —— 压到刚好卡线的话下一步立刻又超，
    压缩就会每一步都改写历史，把 prompt 前缀缓存打碎（有实测数据）。
    """
    before = estimate_tokens(messages) + overhead_tokens
    # 低水位目标：不低于 1K，也不高于预算本身
    target = max(1000, min(int(budget_tokens * float(low_water_ratio)), budget_tokens))
    stats = {
        "before_tokens": before,
        "budget": budget_tokens,
        "target_tokens": target,
        "after_tokens": before,
        "pruned": 0,
        "folded": 0,
        "user_truncated": 0,
        "saved_chars": 0,
        "emergency_pruned": False,
        "pairing_ok": True,
        # 压缩后**是否仍然超预算**。调用方必须看这个字段 ——
        # 原来只检查 pairing_ok，于是「压了但没压下去」的请求照样发出去。
        "over_budget": before > budget_tokens,
    }
    if before <= budget_tokens:
        stats["over_budget"] = False
        return stats

    # Tier 1：剪枝（零成本、结构安全）
    saved = prune_tool_results(messages, keep_recent=keep_recent)
    stats["saved_chars"] += saved
    stats["pruned"] = 1 if saved else 0
    after = estimate_tokens(messages) + overhead_tokens

    # Tier 2：还不够就折叠整段
    if fold and after > target:
        n = fold_old_segments(messages)
        stats["folded"] = n
        after = estimate_tokens(messages) + overhead_tokens

    # Tier 3：兜底 —— 连最近的工具结果也剪。
    # 走到这里说明单个结果太大（或段数太少没法折），只能硬压。
    if after > target:
        saved2 = emergency_prune(messages, target, overhead_tokens)
        stats["saved_chars"] += saved2
        stats["emergency_pruned"] = saved2 > 0
        after = estimate_tokens(messages) + overhead_tokens

    # Tier 4：user / 附件内容。前三层完全不碰它们，于是「粘一段 20 万字符的日志」
    # 可以绕过整套压缩直接触发 400。
    if after > budget_tokens:
        saved3 = prune_user_content(messages)
        stats["saved_chars"] += saved3
        stats["user_truncated"] = 1 if saved3 else 0
        after = estimate_tokens(messages) + overhead_tokens

    stats["after_tokens"] = after
    stats["over_budget"] = after > budget_tokens
    problems = check_pairing(messages)
    stats["pairing_ok"] = not problems
    if problems:
        # 这是**不该发生**的：宁可让调用方知道，也不要悄悄发出一个必然 400 的请求
        logger.error("上下文压缩后配对被破坏：%s", problems[:3])
    if stats["over_budget"]:
        logger.warning(
            "压缩后仍超预算：%d > %d（多半是 system / 单条巨型消息无法再压）",
            after,
            budget_tokens,
        )
    return stats
