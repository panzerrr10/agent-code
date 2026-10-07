"""「中断 → 人工确认 → 断点续跑」全流程测试。

这是项目里最容易出问题的一段逻辑：
- 未确认前绝不能产生副作用（不能先写文件再问用户）
- pending_state 必须保存「已处理的 tool_calls」，否则续跑时 LLM 会报 400
  （assistant 带 tool_calls 却没有对应的 tool 消息）
- 续跑完成后必须清空 pending_state，避免下一轮任务读到脏状态
"""

import json
import os
import shutil
import tempfile
import types

import pytest

import agent
import runtime
import tools

HOME = os.path.expanduser("~")


@pytest.fixture
def project(monkeypatch):
    d = tempfile.mkdtemp(prefix=".agentcode-confirm-", dir=HOME)
    monkeypatch.setattr(tools, "TRASH_DIR", os.path.join(d, ".trash"))
    monkeypatch.setattr(agent, "get_llm_client", lambda *a, **k: object())
    runtime.reset_all()
    runtime.set_session_id("confirm-sess")
    runtime.set_project_root(d)
    yield d
    runtime.reset_all()
    runtime.set_session_id(None)
    shutil.rmtree(d, ignore_errors=True)


# ============ 伪流式响应 ============


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


def _tool_call_chunk(name, args: dict, call_id="call_1"):
    return _Chunk(
        _Delta(
            tool_calls=[
                _TC(0, id=call_id, name=name, arguments=json.dumps(args, ensure_ascii=False))
            ]
        )
    )


def _text_chunk(text):
    return _Chunk(_Delta(content=text))


def _llm_returning(chunks):
    def fake(*args, **kwargs):
        def gen():
            yield from chunks

        return gen()

    return fake


# ============ 用例 ============


def test_write_file_waits_for_confirm_then_resumes(project, monkeypatch):
    target = os.path.join(project, "cfg.txt")
    with open(target, "w", encoding="utf-8") as f:
        f.write("OLD")

    # ---- 第 1 轮：模型要写文件，但必须停下等确认 ----
    monkeypatch.setattr(
        agent,
        "call_llm_with_retry",
        _llm_returning([_tool_call_chunk("write_file", {"path": target, "content": "NEW"})]),
    )

    events = list(
        agent.run_agent_stream("改一下配置", session_id="confirm-sess", user_api_key="k")
    )
    types = [e["type"] for e in events]

    assert "confirm_write" in types, f"应请求确认，实际事件：{types}"
    ev = next(e for e in events if e["type"] == "confirm_write")
    assert ev["path"] == target
    assert ev["new_content"] == "NEW"
    assert ev["old_content"] == "OLD"  # 确认框里能看到新旧对比

    # 未确认 → 绝不能产生副作用
    with open(target, encoding="utf-8") as f:
        assert f.read() == "OLD", "用户在确认前文件就被改了"

    # pending_state 必须落好，且带上待执行工具
    st = runtime.get_pending_state()
    assert st is not None, "未保存 pending_state，用户点确认后将无法续跑"
    assert st["pending_tool"]["name"] == "write_file"
    assert st["pending_tool"]["args"]["content"] == "NEW"
    assert st["step"] == 0
    assert st["messages"], "pending_state 里必须带上完整 messages 才能续跑"

    # ---- 第 2 轮：用户点 ✓ → 续跑，工具真正执行 ----
    monkeypatch.setattr(
        agent, "call_llm_with_retry", _llm_returning([_text_chunk("已经改好了")])
    )

    events2 = list(
        agent.run_agent_stream(
            "改一下配置",
            session_id="confirm-sess",
            _resume=True,
            confirmed_delete=True,
            user_api_key="k",
        )
    )
    types2 = [e["type"] for e in events2]

    with open(target, encoding="utf-8") as f:
        assert f.read() == "NEW", "确认后应真正写入"

    assert "tool_result" in types2, f"续跑应回填工具结果，实际：{types2}"
    assert any(
        e["type"] == "text" and e.get("content") == "已经改好了" for e in events2
    ), types2
    assert types2[-1] == "done"
    assert runtime.get_pending_state() is None, "续跑结束后应清空 pending_state"


def test_delete_file_waits_for_confirm(project, monkeypatch):
    target = os.path.join(project, "dead.txt")
    with open(target, "w", encoding="utf-8") as f:
        f.write("x")

    monkeypatch.setattr(
        agent,
        "call_llm_with_retry",
        _llm_returning([_tool_call_chunk("delete_file", {"path": target})]),
    )
    events = list(
        agent.run_agent_stream("删掉它", session_id="confirm-sess", user_api_key="k")
    )
    types = [e["type"] for e in events]

    assert "confirm_delete" in types, types
    assert os.path.exists(target), "未经确认就删除了文件"
    assert runtime.get_pending_state()["pending_tool"]["name"] == "delete_file"


def test_unsafe_command_waits_for_confirm_but_readonly_runs(project, monkeypatch):
    """只读命令免确认直接跑；写类命令必须等确认。"""
    monkeypatch.setattr(
        agent,
        "call_llm_with_retry",
        _llm_returning([_tool_call_chunk("run_command", {"cmd": "mkdir hacked"})]),
    )
    events = list(
        agent.run_agent_stream("建个目录", session_id="confirm-sess", user_api_key="k")
    )
    types = [e["type"] for e in events]
    assert "confirm_command" in types, types
    assert not os.path.exists(os.path.join(project, "hacked"))


def test_pending_state_is_per_session(project, monkeypatch):
    """A 会话挂起的确认，不能被 B 会话读到（多标签页场景）。"""
    target = os.path.join(project, "a.txt")
    with open(target, "w", encoding="utf-8") as f:
        f.write("OLD")

    monkeypatch.setattr(
        agent,
        "call_llm_with_retry",
        _llm_returning([_tool_call_chunk("write_file", {"path": target, "content": "X"})]),
    )
    list(agent.run_agent_stream("改文件", session_id="sessA", user_api_key="k"))

    runtime.set_session_id("sessB")
    assert runtime.get_pending_state() is None, "B 会话不应看到 A 会话挂起的确认"
    runtime.set_session_id("sessA")
    assert runtime.get_pending_state() is not None
