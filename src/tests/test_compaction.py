
"""上下文压缩的测试。

最要紧的一条：**压缩后 tool_call / tool 消息必须仍然配对**。
配对一旦破坏，发给 LLM 必然 400，而且错误信息很难看懂
（「assistant 消息带 tool_calls 却没有对应 tool 响应」）。
"""

import json
import sys
import types

from tests.fakes import patch_agent

sys.path.insert(0, "src")

import agent  # noqa: E402
import compaction as C  # noqa: E402

# ============ 构造样例 ============


def build(steps=20, big=6000, system=True):
    """造一段像真实运行的 messages。"""
    msgs = []
    if system:
        msgs.append({"role": "system", "content": "你是 AI Agent。"})
    msgs.append({"role": "user", "content": "把项目里的 TODO 都修掉"})
    for i in range(steps):
        cid = f"call_{i}"
        msgs.append({
            "role": "assistant", "content": None,
            "tool_calls": [{"id": cid, "type": "function", "function": {
                "name": "read_file", "arguments": json.dumps({"path": f"src/m{i}.py"})}}],
        })
        msgs.append({"role": "tool", "tool_call_id": cid, "content": "X" * big})
    msgs.append({"role": "assistant", "content": "做完了"})
    return msgs


# ============ 估算与配对 ============


def test_estimate_tokens_counts_text():
    msgs = [{"role": "user", "content": "a" * 3000}]
    assert C.estimate_tokens(msgs) == 1000


def test_estimate_tokens_counts_tool_calls():
    msgs = [{"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "function": {"name": "read_file", "arguments": "{}"}}]}]
    assert C.estimate_tokens(msgs) > 0


def test_estimate_tokens_multimodal():
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "a" * 300}, {"type": "image_url", "image_url": {"url": "x"}}]}]
    assert C.estimate_tokens(msgs) > 100      # 图片按固定值计


def test_check_pairing_accepts_valid():
    assert C.check_pairing(build(3)) == []


def test_check_pairing_detects_missing_tool_reply():
    m = build(3)
    del m[3]                                   # 删掉一条 tool 消息
    problems = C.check_pairing(m)
    assert problems and "没有对应 tool 消息" in problems[0]


def test_check_pairing_detects_orphan_tool():
    m = build(2)
    m.append({"role": "tool", "tool_call_id": "ghost", "content": "x"})
    problems = C.check_pairing(m)
    assert any("找不到对应的 assistant" in p for p in problems)


# ============ Tier 1：工具结果剪枝 ============


def test_prune_keeps_recent_full():
    m = build(steps=20)
    C.prune_tool_results(m, keep_recent=8)
    tools = [x for x in m if x["role"] == "tool"]
    assert len(tools) == 20
    assert all(len(t["content"]) > 100 for t in tools[-8:]), "最近 8 条必须保留全文"
    assert tools[0]["content"].startswith("["), "更旧的应变成占位"


def test_prune_only_changes_content_not_structure():
    """**核心不变量**：剪枝绝不改变消息数量与 tool_call_id。"""
    m = build(steps=12)
    before = [(x.get("role"), x.get("tool_call_id")) for x in m]
    C.prune_tool_results(m, keep_recent=4)
    after = [(x.get("role"), x.get("tool_call_id")) for x in m]
    assert before == after, "剪枝不能增删消息，也不能改 tool_call_id"
    assert C.check_pairing(m) == []


def test_prune_reduces_tokens():
    m = build(steps=20)
    before = C.estimate_tokens(m)
    C.prune_tool_results(m, keep_recent=8)
    assert C.estimate_tokens(m) < before * 0.7


def test_prune_is_idempotent():
    m = build(steps=20)
    C.prune_tool_results(m, keep_recent=8)
    once = C.estimate_tokens(m)
    C.prune_tool_results(m, keep_recent=8)
    assert C.estimate_tokens(m) == once, "再剪一次不应有变化"


def test_prune_skips_short_results():
    """很短的结果剪了也省不了多少，还会丢信息，不如留着。"""
    m = build(steps=12, big=50)
    C.prune_tool_results(m, keep_recent=2)
    tools = [x for x in m if x["role"] == "tool"]
    assert all(not t["content"].startswith("[") for t in tools)


def test_prune_labels_the_tool_name():
    m = build(steps=12)
    C.prune_tool_results(m, keep_recent=2)
    tools = [x for x in m if x["role"] == "tool"]
    assert "read_file" in tools[0]["content"], "占位里要写清是哪个工具，否则模型看不懂"


# ============ Tier 2：成段折叠 ============


def test_fold_removes_whole_segments_and_keeps_pairing():
    m = build(steps=30)
    n = C.fold_old_segments(m, keep_recent=6)
    assert n > 0
    assert C.check_pairing(m) == [], "折叠必须保持配对"


def test_fold_keeps_original_task():
    m = build(steps=30)
    C.fold_old_segments(m, keep_recent=6)
    assert m[1]["content"] == "把项目里的 TODO 都修掉", "原始任务绝不能丢"
    assert m[0]["role"] == "system"


def test_fold_inserts_digest():
    m = build(steps=30)
    C.fold_old_segments(m, keep_recent=6)
    digests = [x for x in m if "已折叠" in (x.get("content") or "")]
    assert len(digests) == 1
    assert "read_file" in digests[0]["content"], "摘要要说明做过什么，否则模型会重复劳动"


def test_fold_keeps_recent_segments():
    m = build(steps=30)
    C.fold_old_segments(m, keep_recent=6)
    tools = [x for x in m if x["role"] == "tool"]
    assert len(tools) >= 6, f"最近 6 段要留着，实际只剩 {len(tools)}"


def test_fold_noop_when_few_segments():
    m = build(steps=3)
    assert C.fold_old_segments(m, keep_recent=6) == 0


def test_fold_without_user_message_is_safe():
    """没有 user 消息时不能崩，也不能把 system 折掉。"""
    m = [{"role": "system", "content": "sys"}]
    for i in range(10):
        m.append({"role": "assistant", "content": f"a{i}"})
    assert C.fold_old_segments(m, keep_recent=2) == 0
    assert m[0]["role"] == "system"


# ============ 端到端 compact ============


def test_compact_noop_under_budget():
    m = build(steps=2)
    st = C.compact(m, budget_tokens=32000)
    assert st["after_tokens"] == st["before_tokens"]
    assert st["folded"] == 0


def test_compact_brings_into_budget():
    m = build(steps=40, big=8000)
    st = C.compact(m, budget_tokens=32000, overhead_tokens=2000)
    assert st["before_tokens"] > st["budget"]
    assert st["after_tokens"] <= st["budget"], st
    assert st["pairing_ok"] is True


def test_compact_accounts_for_overhead():
    """工具 schema 不在这份 messages 里但每次都要发，必须计入预算。"""
    m = build(steps=6, big=3000)
    a = C.compact(build(steps=6, big=3000), budget_tokens=20000, overhead_tokens=0)
    b = C.compact(m, budget_tokens=20000, overhead_tokens=1800)
    assert b["after_tokens"] >= a["after_tokens"]


def test_compact_pairing_always_ok():
    """各种规模下压完都不能破坏配对。"""
    for steps, big in ((10, 1000), (30, 5000), (50, 12000), (5, 80000)):
        m = build(steps=steps, big=big)
        st = C.compact(m, budget_tokens=32000, overhead_tokens=1800)
        assert C.check_pairing(m) == [], f"steps={steps} big={big} 压坏了配对"
        assert st["pairing_ok"] is True


def test_compact_handles_huge_single_tool_result():
    """单条巨大的工具结果也能处理掉。"""
    m = build(steps=1, big=300000)
    st = C.compact(m, budget_tokens=32000)
    assert st["after_tokens"] <= 32000, st
    assert C.check_pairing(m) == []


def test_emergency_prune_handles_single_huge_recent_result():
    """keep_recent 保护了最近的结果，但**那一条本身可能就是巨型的**。

    真实场景：读了一个几十万字符的文件，光它一个就撑爆预算。
    这时只能连它一起压 —— 总比发一个必然 400 的请求好。
    """
    m = build(steps=3, big=200000)
    st = C.compact(m, budget_tokens=32000)
    assert st["after_tokens"] <= 32000, st
    assert st["emergency_pruned"] is True, "应当走兜底剪枝"
    assert C.check_pairing(m) == []


def test_emergency_prune_stops_when_under_budget():
    """进入预算后就该停手，不要一路剪光。"""
    m = build(steps=6, big=40000)
    C.compact(m, budget_tokens=30000)
    tools = [x for x in m if x["role"] == "tool"]
    intact = [t for t in tools if not t["content"].startswith("[")]
    assert intact, "不该把所有工具结果都剪光"


def test_compact_never_drops_first_user():
    m = build(steps=50, big=9000)
    C.compact(m, budget_tokens=20000)
    users = [x for x in m if x["role"] == "user"]
    assert users and users[0]["content"] == "把项目里的 TODO 都修掉"


# ============ 与 agent 主循环的集成 ============


class _TC:
    def __init__(self, index, id, name, arguments):
        self.index = index
        self.id = id
        self.function = types.SimpleNamespace(name=name, arguments=arguments)


class _Delta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning_content = None


class _Chunk:
    def __init__(self, delta):
        self.choices = [type("C", (), {"delta": delta})()]
        self.usage = None


def _tool_call(idx, name, args):
    return _Chunk(_Delta(tool_calls=[_TC(idx, f"call_{idx}", name, json.dumps(args))]))


def _final(text):
    return _Chunk(_Delta(content=text))


def _stream(*chunks):
    state = {"i": 0}

    def fake(*args, **kwargs):
        i = min(state["i"], len(chunks) - 1)
        state["i"] += 1
        state["calls"] = state["i"]

        def gen():
            yield chunks[i]

        return gen()

    fake.state = state
    return fake


def test_agent_emits_context_stats_when_over_budget(client, monkeypatch):
    """真正跑一遍主循环：预算调小后必须发出 context_stats 事件。"""
    sid = client.post("/sessions", json={}).get_json()["id"]
    monkeypatch.setattr(agent, "CONTEXT_BUDGET_TOKENS", 500)
    monkeypatch.setattr(agent, "CONTEXT_KEEP_RECENT_TOOL_RESULTS", 1)
    # 第一步读个大文件，第二步收尾
    patch_agent(monkeypatch, _stream(
        _tool_call(0, "list_dir", {"path": "."}),
        _final("好的"),
    ))
    monkeypatch.setattr(agent, "execute_tool", lambda n, a: "Y" * 20000)

    events = list(agent.run_agent_stream("随便干点啥", session_id=sid, user_api_key="k"))
    stats = [e for e in events if e.get("type") == "context_stats"]
    assert stats, "超预算时必须发出 context_stats，否则用户看不到压缩在发生"
    assert stats[0]["budget"] == 500
    assert stats[0]["before_tokens"] > stats[0]["after_tokens"] or stats[0]["folded"]


def test_agent_no_context_stats_when_under_budget(client, monkeypatch):
    sid = client.post("/sessions", json={}).get_json()["id"]
    patch_agent(monkeypatch, _stream(_final("直接回答")))
    events = list(agent.run_agent_stream("你好", session_id=sid, user_api_key="k"))
    assert not [e for e in events if e.get("type") == "context_stats"], "没超预算不该报压缩"
