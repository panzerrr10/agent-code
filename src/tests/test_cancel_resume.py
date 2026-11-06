"""「中断后可从断点继续」+「工具级取消」的回归测试。

覆盖三条链路：
1. 取消时会把断点写进 pending_state（以前只有「等待确认」才写，导致中断=作废）
2. 取消点落在「工具执行到一半」时，必须裁掉未处理的 tool_calls，否则续跑 400
3. 续跑门控：挂起确认要令牌，纯「继续」免令牌
"""

import json
import types

import pytest

import agent
import app as app_module
import runtime
import tools

PROVIDER_CFG = {
    "api_key": "sk-test",
    "base_url": "https://api.deepseek.com/v1",
    "name": "DeepSeek",
    "icon": "🐳",
    "models": ["deepseek-chat"],
    "default_model": "deepseek-chat",
    "supports_embedding": False,
}


@pytest.fixture(autouse=True)
def _clean():
    runtime.reset_all()
    runtime.set_session_id(None)
    yield
    runtime.reset_all()
    runtime.set_session_id(None)


def _assert_no_dangling_tool_calls(messages):
    """每条带 tool_calls 的 assistant 消息，后面必须有等量的 tool 响应。"""
    for i, m in enumerate(messages):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            ids = [t["id"] for t in m["tool_calls"]]
            following = []
            for nxt in messages[i + 1:]:
                if nxt.get("role") == "tool":
                    following.append(nxt.get("tool_call_id"))
                else:
                    break
            missing = [x for x in ids if x not in following]
            assert not missing, f"悬空 tool_calls（续跑会 400）：{ids} vs {following}"


# ---------- 伪流式 ----------


class _Delta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning_content = None


class _Choice:
    def __init__(self, delta):
        self.delta = delta


class _Chunk:
    def __init__(self, delta):
        self.choices = [_Choice(delta)]
        self.usage = None


class _TC:
    def __init__(self, index, id=None, name=None, arguments=None):
        self.index = index
        self.id = id
        self.function = types.SimpleNamespace(name=name, arguments=arguments)


def _llm(chunks):
    def fake(*args, **kwargs):
        def gen():
            yield from chunks

        return gen()

    return fake


def _tool_call_chunks(*calls):
    """一次响应里带多个 tool_call。"""
    tcs = [
        _TC(i, id=f"call_{i}", name=name, arguments=json.dumps(args))
        for i, (name, args) in enumerate(calls)
    ]
    return [_Chunk(_Delta(tool_calls=tcs))]


# ============ 1. 取消会保存断点 ============


def test_cancel_saves_resumable_state(monkeypatch):
    monkeypatch.setattr(runtime, "is_cancelled", lambda sid=None: True)
    monkeypatch.setattr(agent, "get_llm_client", lambda *a, **k: object())

    def boom(*a, **k):
        raise AssertionError("取消后不应再调用 LLM")

    monkeypatch.setattr(agent, "call_llm_with_retry", boom)

    events = list(agent.run_agent_stream("随便干点啥", session_id="s1", user_api_key="k"))
    types = [e["type"] for e in events]

    # 开头的 run_started / project_context / repo_map 都是**信息性事件**
    # （告诉前端本轮上下文里注入了什么），与「取消是否立即生效」无关，先滤掉。
    _informational = {"run_started", "project_context", "repo_map"}
    core = [t for t in types if t not in _informational]
    assert core[0] == "cancelled", types
    assert types[-1] == "done"
    _cancelled = next(e for e in events if e["type"] == "cancelled")
    assert "可从断点继续" in _cancelled["content"]

    state = runtime.get_pending_state("s1")
    assert state is not None, "取消时没有保存断点，用户无法继续"
    assert state.get("pending_tool") is None, "中断续跑不应带 pending_tool（否则会要求确认令牌）"
    assert state["step"] == 0
    assert state["task"] == "随便干点啥"
    assert state["messages"], "必须带上完整 messages 才能续跑"
    _assert_no_dangling_tool_calls(state["messages"])


def test_resume_after_cancel_continues_the_task(monkeypatch):
    flag = {"cancel": True}
    monkeypatch.setattr(runtime, "is_cancelled", lambda sid=None: flag["cancel"])
    monkeypatch.setattr(agent, "get_llm_client", lambda *a, **k: object())

    # 第一次：被取消，存下断点
    list(agent.run_agent_stream("写点东西", session_id="s2", user_api_key="k"))
    assert runtime.get_pending_state("s2") is not None

    # 第二次：用户点「继续」
    flag["cancel"] = False
    monkeypatch.setattr(agent, "call_llm_with_retry", _llm([_Chunk(_Delta(content="收尾完成"))]))
    events = list(
        agent.run_agent_stream("写点东西", session_id="s2", _resume=True, user_api_key="k")
    )
    types = [e["type"] for e in events]

    assert "cancelled" not in types
    assert any(e["type"] == "text" and e.get("content") == "收尾完成" for e in events), types
    assert types[-1] == "done"
    assert runtime.get_pending_state("s2") is None, "续跑完成后应清掉断点"


# ============ 2. 工具中途取消：不能留下悬空 tool_calls ============


def test_cancel_between_tools_keeps_message_sequence_valid(monkeypatch):
    """两个 tool_call，执行完第一个后用户点停止：
    保存的 messages 必须只保留已处理的那个，否则续跑时 LLM 直接 400。"""
    monkeypatch.setattr(agent, "get_llm_client", lambda *a, **k: object())
    monkeypatch.setattr(
        agent,
        "call_llm_with_retry",
        _llm(_tool_call_chunks(("list_dir", {"path": "."}), ("get_file_info", {"path": "."}))),
    )

    # 第一个工具执行时触发取消，这样第二个工具前的检查点就会命中
    def first_tool(path="."):
        runtime.request_cancel("s3")
        return "✅ 列目录完成"

    monkeypatch.setitem(tools.TOOL_FUNCTIONS, "list_dir", first_tool)

    events = list(agent.run_agent_stream("看看目录", session_id="s3", user_api_key="k"))
    types = [e["type"] for e in events]
    assert "cancelled" in types, types

    state = runtime.get_pending_state("s3")
    assert state is not None

    msgs = state["messages"]
    _assert_no_dangling_tool_calls(msgs)

    # 当前这条 assistant 的未处理 tool_call 应被裁掉
    assistant_tcs = [m for m in msgs if m.get("role") == "assistant" and m.get("tool_calls")]
    assert assistant_tcs, "已处理的 tool_call 不该被整条删除"
    kept = [t["id"] for t in assistant_tcs[-1]["tool_calls"]]
    assert kept == ["call_0"], f"只应保留已处理的 call_0，实际 {kept}"


# ============ 3. 续跑门控（HTTP）============


def _stub_provider(monkeypatch):
    monkeypatch.setattr(app_module, "get_provider", lambda pid=None: dict(PROVIDER_CFG))
    monkeypatch.setattr(agent, "get_provider", lambda pid=None: dict(PROVIDER_CFG))
    monkeypatch.setattr(agent, "get_llm_client", lambda *a, **k: object())


def test_resume_without_state_is_rejected(client, monkeypatch):
    _stub_provider(monkeypatch)
    sid = client.post("/sessions").get_json()["id"]

    resp = client.post("/run", json={"task": "", "session_id": sid, "resume": True})
    events = [
        json.loads(line[6:])
        for line in resp.get_data(as_text=True).splitlines()
        if line.startswith("data: ")
    ]
    assert events and events[0]["type"] == "error"
    assert "没有可续跑的任务" in events[0]["content"]


def test_cancel_resume_needs_no_confirm_token(client, monkeypatch):
    """中断续跑不是「确认危险操作」，因此不应该被要求一次性令牌。"""
    _stub_provider(monkeypatch)
    sid = client.post("/sessions").get_json()["id"]

    # 模拟「中断时保存的断点」：没有 pending_tool
    runtime.set_pending_state(
        {"messages": [{"role": "user", "content": "任务"}], "step": 3, "task": "任务"},
        sid,
    )
    monkeypatch.setattr(agent, "call_llm_with_retry", _llm([_Chunk(_Delta(content="接着干完"))]))

    resp = client.post(
        "/run", json={"task": "", "session_id": sid, "resume": True, "project_root": None}
    )
    events = [
        json.loads(line[6:])
        for line in resp.get_data(as_text=True).splitlines()
        if line.startswith("data: ")
    ]
    types = [e["type"] for e in events]

    assert "error" not in types, f"纯「继续」被误判为未授权：{events[:2]}"
    assert types[-1] == "done"
    client.delete(f"/sessions/{sid}")


def test_new_task_clears_previous_resume_point(client, monkeypatch):
    """开了新任务后，旧的断点应被清掉，避免「继续」接到上一条任务。"""
    _stub_provider(monkeypatch)
    sid = client.post("/sessions").get_json()["id"]
    runtime.set_pending_state({"messages": [], "step": 1, "task": "旧任务"}, sid)

    monkeypatch.setattr(agent, "call_llm_with_retry", _llm([_Chunk(_Delta(content="新任务完成"))]))
    client.post(
        "/run", json={"task": "新任务", "session_id": sid, "provider_id": "deepseek"}
    )

    assert runtime.get_pending_state(sid) is None
    client.delete(f"/sessions/{sid}")
