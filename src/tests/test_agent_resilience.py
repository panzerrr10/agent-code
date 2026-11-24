"""Agent 主循环的健壮性修复（本次改动）。

对应三个实测缺陷：

1. **失败熔断不是「连续」失败**：复位分支要求 ``result.startswith("❌")`` 且只在
   非特权工具上生效，于是特权工具**成功时计数器不清零** ——
   「失败→成功→失败→成功→失败」也会触发中止，提示却写着「已连续失败 3 次」。
2. **报错不落 pending**：第 20 步遇到一次 429 / 网络抖动（重试 3 次仍失败）
   就意味着**前 19 步的工具结果全丢**，用户只能重说一遍。
3. **失败警告插错位置**：警告被当成独立 ``user`` 消息插在
   ``assistant(tool_calls)`` 与它的 ``tool`` 回复之间，造成非法交错 ——
   违反 OpenAI 契约，并会让压缩的配对自检失败（而自检失败会中止整轮任务）。
"""

import itertools
import json
import types

import pytest

import agent
import compaction
from tests.fakes import patch_agent

# ============ 流式假件 ============


class _TC:
    def __init__(self, idx, cid, name, args):
        self.index = idx
        self.id = cid
        self.type = "function"
        self.function = types.SimpleNamespace(name=name, arguments=args)


class _Delta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning_content = None


class _Chunk:
    def __init__(self, delta):
        self.choices = [types.SimpleNamespace(delta=delta)]
        self.usage = None


# 每个 tool_call 用**唯一 id**：真实 LLM 每次调用都会给新 id，
# 复用同一个 id 会造出「同一条 assistant 消息」的假象，把配对检查搞乱。
_tc_seq = itertools.count()


def _tool_call(idx, name, args):
    return _Chunk(
        _Delta(tool_calls=[_TC(idx, f"call_{next(_tc_seq)}", name, json.dumps(args))])
    )


def _final(text):
    return _Chunk(_Delta(content=text))


def _stream(*chunks):
    """按次序返回 chunk 的假 LLM；用完后一直重放最后一个。"""
    state = {"i": 0}

    def fake(*args, **kwargs):
        i = min(state["i"], len(chunks) - 1)
        state["i"] += 1
        return iter([chunks[i]])

    fake.state = state
    return fake


def _repeat(chunk, n=8):
    return [chunk for _ in range(n)]


@pytest.fixture
def env(monkeypatch):
    """建一个**等级 3** 的会话。

    这些用例测的是熔断 / 断点，不是确认流程。等级 2 下 write_file 会先弹确认
    卡片并 return，根本走不到失败计数那一段（踩过一次：测试看起来在测熔断，
    其实每次都停在确认挂起处）。
    """
    import db
    import runtime

    runtime.reset_all()
    sid = db.create_session("韧性测试")
    db.set_permission_level(sid, 3)
    runtime.reset_all()
    yield runtime, sid
    runtime.reset_all()


def _run(sid, task="干活", **kw):
    return list(agent.run_agent_stream(task, session_id=sid, user_api_key="k", **kw))


def _texts(events):
    return "".join(e.get("content", "") for e in events if e.get("type") == "text")


# ============ 1. 熔断计数必须「成功即清零」 ============


def test_failure_counter_resets_on_success(env, monkeypatch):
    """失败→成功→失败→成功→失败 不该触发中止（这不是「连续失败」）。"""
    runtime, sid = env
    results = iter(["❌ 失败", "✅ 成功", "❌ 失败", "✅ 成功", "❌ 失败", "✅ 成功"])
    monkeypatch.setattr(agent, "execute_tool", lambda n, a: next(results, "✅ 成功"))
    patch_agent(monkeypatch, _stream(*_repeat(_tool_call(0, "write_file", {"path": "a.py", "content": "x"}))),
    )

    events = _run(sid)
    assert "已连续失败 3 次" not in _texts(events), "成功过的失败计数必须清零"


def test_three_consecutive_failures_still_stop(env, monkeypatch):
    """真正的连续失败仍要熔断 —— 别把修复做成「永不熔断」。"""
    runtime, sid = env
    monkeypatch.setattr(agent, "execute_tool", lambda n, a: "❌ 一直失败")
    patch_agent(monkeypatch, _stream(*_repeat(_tool_call(0, "write_file", {"path": "a.py", "content": "x"}))),
    )
    assert "已连续失败 3 次" in _texts(_run(sid))


# ============ 2. 失败警告必须并进 tool 结果 ============


def test_failure_warning_keeps_pairing_valid(env, monkeypatch):
    """两次失败后 messages 里不能出现 assistant→user→tool 的非法交错。"""
    runtime, sid = env
    monkeypatch.setattr(agent, "execute_tool", lambda n, a: "❌ 写入失败")
    patch_agent(monkeypatch, _stream(*_repeat(_tool_call(0, "write_file", {"path": "a.py", "content": "x"}))),
    )
    _run(sid)

    state = runtime.get_pending_state(sid)
    assert state is not None
    assert compaction.check_pairing(state["messages"]) == [], (
        "失败警告若被插成独立 user 消息，配对就会破坏 → 整轮任务被中止"
    )
    tool_msgs = [m for m in state["messages"] if m.get("role") == "tool"]
    assert any("系统提示" in str(m.get("content", "")) for m in tool_msgs), (
        "警告要仍然传达给模型，只是换成拼进 tool 结果的合法形式"
    )


# ============ 3. 报错落 pending ============


def test_llm_error_saves_resume_point(env, monkeypatch):
    """一次 429 不该让前面所有步骤白跑。"""
    runtime, sid = env

    def boom(*a, **k):
        raise RuntimeError("429 rate limit exceeded")

    patch_agent(monkeypatch, boom)

    events = _run(sid)
    assert any(e["type"] == "error" for e in events)

    state = runtime.get_pending_state(sid)
    assert state is not None, "报错必须存断点，否则用户只能重说一遍"
    assert state["task"] == "干活"


def test_context_length_error_gets_friendly_message(env, monkeypatch):
    runtime, sid = env

    def boom(*a, **k):
        raise RuntimeError("400 This model's maximum context length is 65536 tokens")

    patch_agent(monkeypatch, boom)
    err = next(e for e in _run(sid) if e["type"] == "error")
    assert "上下文" in err["content"]


# ============ 4. 每一步都写断点 ============


def test_each_step_persists_resume_point(env, monkeypatch):
    """正常收尾仍要清掉断点（不能因为多写了几次就留下脏状态）。"""
    runtime, sid = env
    monkeypatch.setattr(agent, "execute_tool", lambda n, a: "ok")
    patch_agent(monkeypatch, _stream(
            _tool_call(0, "list_dir", {"path": "."}),
            _tool_call(0, "list_dir", {"path": "."}),
            _final("完成"),
        ),
    )
    _run(sid)
    assert runtime.get_pending_state(sid) is None


def test_pending_saved_before_llm_call(env, monkeypatch):
    """断点必须在**调用 LLM 之前**写好（否则报错时来不及存）。"""
    runtime, sid = env
    captured = {}

    def boom(*a, **k):
        captured["state"] = runtime.get_pending_state(sid)
        raise RuntimeError("connection reset")

    patch_agent(monkeypatch, boom)
    _run(sid)
    assert captured["state"] is not None, "LLM 调用前就该有断点了"


# ============ 5. 压缩配对自愈 ============


def test_pairing_repair_is_wired_in(env, monkeypatch):
    """压缩若真把配对搞坏，agent 必须先尝试自愈而不是直接中止整轮。"""
    runtime, sid = env
    calls = {"n": 0}
    real_repair = compaction.repair_pairing

    def spy(messages):
        calls["n"] += 1
        return real_repair(messages)

    # 让 compact 谎报配对被破坏，强制走自愈分支
    real_compact = agent.compaction.compact

    def fake_compact(messages, **kw):
        stats = real_compact(messages, **kw)
        stats["pairing_ok"] = False
        return stats

    monkeypatch.setattr(agent.compaction, "compact", fake_compact)
    monkeypatch.setattr(compaction, "repair_pairing", spy)
    monkeypatch.setattr(agent, "execute_tool", lambda n, a: "ok")
    patch_agent(monkeypatch, _stream(_tool_call(0, "list_dir", {"path": "."}), _final("好了")),
    )

    events = _run(sid)
    assert calls["n"] >= 1, "配对被破坏时必须走自愈"
    assert "内部错误" not in _texts(events), "能自愈就不该中止整轮"
    assert any(e["type"] == "done" for e in events)
