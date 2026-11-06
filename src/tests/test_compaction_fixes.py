"""上下文压缩的三个 P0 修复 + 配对自愈的回归测试。

这三个都是**实测复现过**的真缺陷（改动前）：

1. 「已压缩」哨兵用 ``content.startswith("[")`` 判断 → 以 ``[`` 开头的工具输出
   （``run_python`` 打印列表、MCP 返回 JSON 数组）永远剪不掉。
   实测一条 30 万字符的结果压缩后 ``after_tokens=101,930``（预算 32,000），
   而 ``pairing_ok`` 还是 True → 带着必然 400 的请求发出去。
2. user / 附件内容完全不可压，且调用方从不检查 ``after_tokens > budget``
   → 粘一段 20 万字符的日志（开发者最高频动作）能绕过整套压缩。
   实测 ``[system, user(20万)]`` 压缩后仍是 69,367。
3. ``fold_old_segments`` 会折叠出孤儿 tool 消息 → ``pairing_ok=False``
   → agent 直接**中止整轮任务**，已跑步骤全废。
"""

import compaction as C

# ============ 1. 哨兵 ============


def test_placeholder_uses_distinct_prefix_not_bare_bracket():
    text = C._placeholder("run_python", "x" * 1000, 900)
    assert text.startswith(C.COMPACTED_PREFIX)
    assert C.is_compacted(text) is True


def test_bracket_output_is_compactable():
    """这是 P0-1 的核心：以 [ 开头的大结果必须能被剪掉。"""
    big = "[" + "x" * 300_000
    assert C.is_compacted(big) is False, "真实工具输出不能被误判成已压缩"

    messages = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "任务"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "run_python", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": big},
    ]
    stats = C.compact(messages, budget_tokens=32_000, overhead_tokens=1_900)
    assert stats["after_tokens"] < 32_000, "以 [ 开头的结果压缩后必须进预算"
    assert stats["over_budget"] is False
    assert stats["pairing_ok"] is True
    assert C.is_compacted(messages[-1]["content"]) is True


def test_realistic_bracket_prefixed_outputs_are_not_false_positives():
    for text in ["[1, 2, 3]", "[main.py:12] 命中", "[{...}]"]:
        assert C.is_compacted(text) is False


# ============ 2. 巨型 user 消息 ============


def test_giant_user_message_is_truncated():
    """P0-2：user / 附件内容原来完全绕过压缩。"""
    messages = [
        {"role": "system", "content": "S" * 2_400},
        {"role": "user", "content": "日志" + "x" * 200_000},
    ]
    stats = C.compact(messages, budget_tokens=32_000, overhead_tokens=1_900)
    assert stats["after_tokens"] < 32_000
    assert stats["user_truncated"] == 1
    assert stats["over_budget"] is False
    # user 内容是「保头保尾」截断 —— 标记在中间，用 is_truncated 判
    assert C.is_truncated(messages[1]["content"]) is True


def test_user_truncation_keeps_head_and_tail():
    """保头保尾：头部是需求，尾部最可能带错误信息。"""
    text = "开头需求" + ("中" * 50_000) + "尾部错误堆栈"
    messages = [{"role": "user", "content": text}]
    C.prune_user_content(messages)
    out = messages[0]["content"]
    assert out.startswith("开头需求")
    assert out.endswith("尾部错误堆栈")
    assert len(out) < len(text)


def test_short_user_message_untouched():
    messages = [{"role": "user", "content": "帮我改个 bug"}]
    assert C.prune_user_content(messages) == 0
    assert messages[0]["content"] == "帮我改个 bug"


def test_multimodal_text_part_is_truncated():
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "看这段" + "y" * 100_000},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
            ],
        }
    ]
    C.prune_user_content(messages)
    assert C.is_truncated(messages[0]["content"][0]["text"]) is True
    # 图片块不能被破坏
    assert messages[0]["content"][1]["type"] == "image_url"


def test_over_budget_is_reported():
    """压不下去时必须如实报告 —— 调用方原来只看 pairing_ok。"""
    # system 本身超预算，任何一层都压不动它
    messages = [{"role": "system", "content": "S" * 100_000}]
    stats = C.compact(messages, budget_tokens=1_000)
    assert stats["over_budget"] is True


def test_under_budget_reports_not_over():
    messages = [{"role": "system", "content": "S"}, {"role": "user", "content": "hi"}]
    stats = C.compact(messages, budget_tokens=32_000)
    assert stats["over_budget"] is False
    assert stats["user_truncated"] == 0


# ============ 3. 孤儿 tool 消息与配对自愈 ============


def _interleaved_messages():
    """复刻旧版 agent.py 造出的非法交错：

    ``assistant(tool_calls) → user(失败警告) → tool``
    （旧代码把「工具连续失败 2 次」的警告当成独立 user 消息插在中间）
    """
    messages = [
        {"role": "system", "content": "S" * 100},
        {"role": "user", "content": "任务"},
    ]
    for i in range(6):
        messages.append({
            "role": "assistant", "content": "",
            "tool_calls": [{"id": f"p{i}", "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"}}],
        })
        messages.append({"role": "tool", "tool_call_id": f"p{i}", "content": "Y" * 4000})
    messages.append({
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "write_file", "arguments": "{}"}}],
    })
    messages.append({"role": "user", "content": "⚠️ 系统提示：write_file 已连续失败 2 次。"})
    messages.append({"role": "tool", "tool_call_id": "c1", "content": "❌ 写入失败"})
    for i in range(6):
        messages.append({
            "role": "assistant", "content": "",
            "tool_calls": [{"id": f"q{i}", "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"}}],
        })
        messages.append({"role": "tool", "tool_call_id": f"q{i}", "content": "Z" * 4000})
    return messages


def test_fold_never_orphans_tool_message():
    """P0-3：折叠不能留下「找不到 assistant 的 tool 消息」。

    改动前这里是 ``pairing_ok=False``，而 agent 收到 False 会**中止整轮任务**。
    """
    messages = _interleaved_messages()
    stats = C.compact(messages, budget_tokens=5_000, overhead_tokens=100)
    assert stats["pairing_ok"] is True
    assert stats["after_tokens"] < 16_527, "该折的还是要折，不能因为怕破坏配对就不作为"


def test_fold_skips_unsafe_segment_but_folds_others():
    messages = _interleaved_messages()
    folded = C.fold_old_segments(messages, keep_recent=0)
    assert folded >= 1, "安全的那几段仍然要折"
    assert C.check_pairing(messages) == []


def test_repair_pairing_removes_orphan_tool():
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "a1", "type": "function", "function": {"name": "x", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "orphan", "content": "没爹的结果"},
    ]
    fixed = C.repair_pairing(messages)
    assert fixed >= 1
    assert C.check_pairing(messages) == []


def test_repair_pairing_drops_unanswered_tool_calls():
    messages = [
        {"role": "assistant", "content": "我想调工具", "tool_calls": [
            {"id": "a1", "type": "function", "function": {"name": "x", "arguments": "{}"}}
        ]},
    ]
    C.repair_pairing(messages)
    assert "tool_calls" not in messages[0]
    assert messages[0]["content"] == "我想调工具", "文本内容必须保留"
    assert C.check_pairing(messages) == []


def test_repair_pairing_keeps_valid_pairs():
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "a1", "type": "function", "function": {"name": "x", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "a1", "content": "ok"},
    ]
    before = C.check_pairing(messages)
    assert C.repair_pairing(messages) == 0
    assert C.check_pairing(messages) == before == []


def test_repair_pairing_partial_tool_calls():
    """一次返回多个 tool_call，只有部分被回复 —— 只摘掉没回复的那个。"""
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "a1", "type": "function", "function": {"name": "x", "arguments": "{}"}},
            {"id": "a2", "type": "function", "function": {"name": "y", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "a1", "content": "ok"},
    ]
    C.repair_pairing(messages)
    ids = [tc["id"] for tc in messages[0]["tool_calls"]]
    assert ids == ["a1"]
    assert C.check_pairing(messages) == []


def test_repair_pairing_handles_empty():
    assert C.repair_pairing([]) == 0
    assert C.repair_pairing(None) == 0


# ============ 4. 低水位：减少压缩频率 = 少改写前缀 = prompt 缓存能命中 ============
#
# 实测背景：长任务里「压到刚好卡预算线」意味着**下一步立刻又超**，
# 于是压缩每一步都触发、每一步都去改写历史中段，把 prompt 前缀缓存打碎。
# 真实跑一个 50 步任务实测：聚合命中率只有 61%，且几乎每两步就锯齿式暴跌一次
# （17 次前缀被打回）。改成「压到 budget × 0.6」后，压缩频率降到约 1/3。


def _grow(messages, step, tool_chars=18000):
    messages.append({
        "role": "assistant", "content": "",
        "tool_calls": [{"id": f"c{step}", "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"}}],
    })
    messages.append({"role": "tool", "tool_call_id": f"c{step}", "content": "X" * tool_chars})


def test_target_tokens_is_reported():
    messages = [{"role": "system", "content": "S" * 2400}]
    _grow(messages, 0)
    stats = C.compact(messages, budget_tokens=32000, overhead_tokens=1900)
    assert stats["target_tokens"] == int(32000 * C.LOW_WATER_RATIO)
    assert stats["target_tokens"] < stats["budget"]


def test_low_water_marks_reduces_compaction_count():
    """同样 30 步，低水位必须显著少触发压缩（这是减少前缀改写的直接手段）。"""
    def count(low_water, steps=30):
        m = [{"role": "system", "content": "S" * 2400},
             {"role": "user", "content": "任务"}]
        n = 0
        for step in range(steps):
            _grow(m, step)
            st = C.compact(m, budget_tokens=32000, overhead_tokens=1900,
                           low_water_ratio=low_water)
            if st["after_tokens"] < st["before_tokens"]:
                n += 1
        return n

    strict = count(1.0)          # 旧行为：压到刚好卡线
    relaxed = count(C.LOW_WATER_RATIO)
    assert relaxed < strict, f"低水位没起作用：{relaxed} vs {strict}"
    assert relaxed <= strict * 0.6, f"降幅不够：{relaxed} vs {strict}"


def test_low_water_actually_compacts_below_target():
    """触发压缩后必须真的一路压到目标水位之下，而不是压一点点就收手。"""
    m = [{"role": "system", "content": "S" * 2400}, {"role": "user", "content": "任务"}]
    for step in range(12):
        _grow(m, step)
    stats = C.compact(m, budget_tokens=32000, overhead_tokens=1900, low_water_ratio=0.6)
    assert stats["after_tokens"] <= stats["target_tokens"] or stats["over_budget"]


def test_low_water_one_reproduces_old_behaviour():
    """ratio=1.0 等价于改动前：压到刚好进预算即可。"""
    m = [{"role": "system", "content": "S" * 2400}, {"role": "user", "content": "任务"}]
    for step in range(12):
        _grow(m, step)
    stats = C.compact(m, budget_tokens=32000, overhead_tokens=1900, low_water_ratio=1.0)
    assert stats["target_tokens"] == 32000
    assert stats["after_tokens"] <= 32000


def test_low_water_ratio_is_clamped():
    """传了离谱的比例也不能把目标搞成 0 或超过预算。"""
    m = [{"role": "system", "content": "S"}, {"role": "user", "content": "任务"}]
    assert C.compact(m, budget_tokens=32000, low_water_ratio=0).get("target_tokens") == 1000
    assert C.compact(m, budget_tokens=32000, low_water_ratio=9).get("target_tokens") == 32000


def test_replay_cache_hit_rate_improves():
    """确定性回放：按真实缓存语义（与历史快照的最长公共前缀）算命中率。

    只改低水位这一个变量，同一个消息序列。这条断言把「省 token」钉成
    一个可回归的数字，而不是靠感觉。
    """
    import copy

    def run(low_water, steps=24):
        m = [{"role": "system", "content": "S" * 2400},
             {"role": "user", "content": "读模块"}]
        snaps, hits, prompts = [], [], []
        for step in range(steps):
            _grow(m, step)
            st = C.compact(m, budget_tokens=32000, overhead_tokens=1900,
                           low_water_ratio=low_water)
            best = 0
            for snap in snaps:
                p = 0
                n = min(len(snap), len(m))
                while p < n and snap[p] == m[p]:
                    p += 1
                best = max(best, C.estimate_tokens(m[:p]) + (1900 if p else 0))
            hits.append(best)
            prompts.append(st["before_tokens"])
            snaps.append(copy.deepcopy(m))
        return sum(hits) / sum(prompts)

    strict = run(1.0)
    relaxed = run(C.LOW_WATER_RATIO)
    assert relaxed > strict + 0.1, f"命中率没改善：{relaxed:.1%} vs {strict:.1%}"
