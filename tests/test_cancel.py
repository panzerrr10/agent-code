"""任务中断（协作式取消）回归测试。

覆盖三层：
1. runtime 取消标志的语义（按会话隔离、重新运行必须清掉旧状态）
2. /run/cancel HTTP 端点
3. Agent 主循环的取消检查点（流式输出中途停止、旧状态不误伤新运行）
"""

import pytest

import agent
import app as app_module
import runtime


@pytest.fixture(autouse=True)
def _clean():
    runtime.reset_all()
    runtime.set_session_id(None)
    yield
    runtime.reset_all()
    runtime.set_session_id(None)


# ============ runtime 层语义 ============


def test_register_run_starts_uncancelled():
    runtime.set_session_id("s1")
    runtime.register_run("s1")
    assert runtime.is_cancelled() is False


def test_request_cancel_only_hits_running_session():
    runtime.register_run("s1")
    assert runtime.request_cancel("s2") is False  # 该会话并没有在跑
    assert runtime.is_cancelled("s1") is False
    assert runtime.request_cancel("s1") is True
    assert runtime.is_cancelled("s1") is True


def test_cancel_is_isolated_per_session():
    runtime.register_run("s1")
    runtime.register_run("s2")
    runtime.request_cancel("s1")
    assert runtime.is_cancelled("s1") is True
    assert runtime.is_cancelled("s2") is False


def test_reregister_clears_previous_cancel_state():
    """最容易踩的坑：复用同一个 Event 会让「新任务刚启动就自杀」。"""
    runtime.register_run("s1")
    runtime.request_cancel("s1")
    assert runtime.is_cancelled("s1") is True

    runtime.register_run("s1")  # 新的一次运行
    assert runtime.is_cancelled("s1") is False


def test_clear_run_removes_flag():
    runtime.register_run("s1")
    runtime.clear_run("s1")
    assert runtime.request_cancel("s1") is False  # 已无运行中的任务


def test_clear_session_also_clears_cancel_flag():
    runtime.register_run("s7")
    runtime.clear_session("s7")
    assert runtime.request_cancel("s7") is False


# ============ HTTP 端点 ============


def test_cancel_endpoint_requires_session_id():
    client = app_module.app.test_client()
    resp = client.post("/run/cancel", json={})
    assert resp.status_code == 400


def test_cancel_endpoint_reports_miss_for_idle_session():
    client = app_module.app.test_client()
    resp = client.post("/run/cancel", json={"session_id": 999999})
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "cancelled": False}


def test_cancel_endpoint_hits_running_session():
    runtime.register_run(4242)
    client = app_module.app.test_client()
    resp = client.post("/run/cancel", json={"session_id": 4242})
    assert resp.get_json()["cancelled"] is True
    assert runtime.is_cancelled(4242) is True


# ============ Agent 主循环检查点 ============


class _FakeDelta:
    def __init__(self, content=None):
        self.content = content
        self.tool_calls = None
        self.reasoning_content = None


class _FakeChoice:
    def __init__(self, delta):
        self.delta = delta


class _FakeChunk:
    def __init__(self, content):
        self.choices = [_FakeChoice(_FakeDelta(content))]
        self.usage = None


def test_agent_stops_mid_stream_when_cancelled(monkeypatch):
    """用户在模型流式输出途中点「停止」→ 立即停下，不再发起新一轮 LLM 调用。"""
    calls = {"n": 0}

    def fake_stream(*args, **kwargs):
        calls["n"] += 1
        yield _FakeChunk("已产出的部分")
        runtime.request_cancel("c2")  # 模拟用户此刻点了「停止」
        yield _FakeChunk("这段不应被处理")

    monkeypatch.setattr(agent, "call_llm_with_retry", fake_stream)

    events = list(agent.run_agent_stream("任务", session_id="c2", user_api_key="k"))
    types = [e.get("type") for e in events]

    assert "cancelled" in types, f"应产出 cancelled 事件，实际：{types}"
    # cancelled 必须在 done 之前
    assert types.index("cancelled") < types.index("done")
    assert types[-1] == "done"
    # 关键：取消后不会再发起第 2 轮 LLM 调用（即不会进入下一步）
    assert calls["n"] == 1, f"取消后仍继续跑，LLM 调用次数={calls['n']}"
    # 取消点之后的 chunk 内容不应出现在任何输出事件里
    assert not any("这段不应被处理" in str(e.get("content", "")) for e in events)


def test_stale_cancel_flag_does_not_abort_new_run(monkeypatch):
    """上一次运行残留的取消状态，不能把新的一次运行直接掐死。"""
    runtime.register_run("c3")
    runtime.request_cancel("c3")
    assert runtime.is_cancelled("c3") is True

    def fake_stream(*args, **kwargs):
        yield _FakeChunk("正常运行的结果")

    monkeypatch.setattr(agent, "call_llm_with_retry", fake_stream)

    events = list(agent.run_agent_stream("任务", session_id="c3", user_api_key="k"))
    types = [e.get("type") for e in events]

    assert "cancelled" not in types, "新运行被旧的取消状态误杀"
    assert types[-1] == "done"
