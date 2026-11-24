"""多 Agent 改造：受限工具循环（mini_agent）+ Planner 探索 + Reviewer 跑测试。

改动前的问题：

- **Planner** 没有工具、不读代码，凭一句任务描述盲出计划，然后 executor 被硬性
  要求「严格按此计划执行，不要重新 create_plan」。对真实代码库，盲订的计划
  经常是错的，而系统还强制你按错的走。
- **Reviewer** 不能读文件、不能跑测试，输入只有 executor **自述的文本**。
  这是典型的「LLM 给 LLM 盖章」，**验证不了任何东西**。
"""

import json
import types

import pytest

import mini_agent
import planner
import policy
import reviewer
import runtime
from tests.fakes import patch_mini


def _msg(content=None, tool_calls=None):
    return types.SimpleNamespace(content=content, tool_calls=tool_calls)


def _resp(message):
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


def _tool_call(cid, name, args="{}"):
    return types.SimpleNamespace(
        id=cid, function=types.SimpleNamespace(name=name, arguments=args)
    )


@pytest.fixture(autouse=True)
def _default_policy(monkeypatch, tmp_path):
    monkeypatch.setattr(policy, "SETTINGS_FILE", str(tmp_path / "policy.json"))
    for env_name in policy.FLAGS.values():
        monkeypatch.delenv(env_name, raising=False)
    policy.reset_cache()
    yield
    policy.reset_cache()


@pytest.fixture
def _fake_client(monkeypatch):
    """兼容旧用例的占位 fixture。

    以前这里负责把 openai 客户端替换掉；现在模型注入由 ``patch_mini`` 在
    每个用例里单独完成，这个 fixture 不再需要做任何事。
    """
    return None


# ============ mini_agent：白名单与权限 ============


def test_disallowed_tool_is_rejected(monkeypatch, _fake_client):
    """模型提到白名单外的工具 → 拒绝并告知，绝不误执行。"""
    calls = []

    def fake_execute(name, args):
        calls.append(name)
        return "执行了"

    monkeypatch.setattr(mini_agent, "execute_tool", fake_execute)
    patch_mini(monkeypatch, lambda *a, **k: _resp(_msg(tool_calls=[_tool_call("1", "write_file", '{"path":"x"}')])),
    )

    out = mini_agent.run_tool_loop(
        system_prompt="s", user_content="u", allowed_tools=["read_file"], max_steps=1
    )
    assert calls == [], "白名单外的工具绝不能被执行"
    assert "write_file" in out["denied"]


def test_allowed_readonly_tool_executes(monkeypatch, _fake_client):
    seen = []

    def fake_execute(name, args):
        seen.append((name, args))
        return "文件内容"

    monkeypatch.setattr(mini_agent, "execute_tool", fake_execute)

    responses = iter([
        _resp(_msg(tool_calls=[_tool_call("1", "read_file", '{"path":"a.py"}')])),
        _resp(_msg(content="看完了")),
    ])
    patch_mini(monkeypatch, lambda *a, **k: next(responses))

    out = mini_agent.run_tool_loop(
        system_prompt="s", user_content="u", allowed_tools=["read_file"], max_steps=3
    )
    assert seen == [("read_file", {"path": "a.py"})]
    assert out["text"] == "看完了"
    assert out["tool_calls"] == [{"name": "read_file", "args": {"path": "a.py"}}]


def test_run_tests_denied_at_level1(monkeypatch, _fake_client):
    """等级 1（只读）下，评审无论如何都不能跑测试。"""
    monkeypatch.setattr(runtime, "get_permission_level", lambda sid=None: 1)
    executed = []
    monkeypatch.setattr(mini_agent, "execute_tool", lambda n, a: executed.append(n) or "ok")
    patch_mini(monkeypatch, lambda *a, **k: _resp(_msg(tool_calls=[_tool_call("1", "run_tests")])),
    )

    out = mini_agent.run_tool_loop(
        system_prompt="s", user_content="u",
        allowed_tools=["read_file", "run_tests"], max_steps=1,
    )
    assert executed == []
    assert "run_tests" in out["denied"]


def test_run_tests_allowed_when_policy_on(monkeypatch, _fake_client):
    monkeypatch.setattr(runtime, "get_permission_level", lambda sid=None: 2)
    policy.set_policy({"allow_reviewer_tests": True})
    executed = []
    monkeypatch.setattr(mini_agent, "execute_tool", lambda n, a: executed.append(n) or "全部通过")
    patch_mini(monkeypatch, lambda *a, **k: _resp(_msg(tool_calls=[_tool_call("1", "run_tests")])),
    )

    mini_agent.run_tool_loop(
        system_prompt="s", user_content="u",
        allowed_tools=["read_file", "run_tests"], max_steps=1,
    )
    assert executed == ["run_tests"]


def test_run_tests_denied_when_policy_off(monkeypatch, _fake_client):
    monkeypatch.setattr(runtime, "get_permission_level", lambda sid=None: 3)
    policy.set_policy({"allow_reviewer_tests": False})
    executed = []
    monkeypatch.setattr(mini_agent, "execute_tool", lambda n, a: executed.append(n) or "ok")
    patch_mini(monkeypatch, lambda *a, **k: _resp(_msg(tool_calls=[_tool_call("1", "run_tests")])),
    )

    out = mini_agent.run_tool_loop(
        system_prompt="s", user_content="u",
        allowed_tools=["read_file", "run_tests"], max_steps=1,
    )
    assert executed == []
    assert "run_tests" in out["denied"]


def test_mutating_tool_denied_even_at_level3(monkeypatch, _fake_client):
    """辅助角色**永远**不许改磁盘 —— 即便等级 3。"""
    monkeypatch.setattr(runtime, "get_permission_level", lambda sid=None: 3)
    executed = []
    monkeypatch.setattr(mini_agent, "execute_tool", lambda n, a: executed.append(n) or "ok")
    patch_mini(monkeypatch, lambda *a, **k: _resp(_msg(tool_calls=[_tool_call("1", "create_file", '{"path":"x","content":"y"}')])),
    )

    out = mini_agent.run_tool_loop(
        system_prompt="s", user_content="u",
        allowed_tools=["read_file", "create_file"], max_steps=1,
    )
    assert executed == []
    assert "create_file" in out["denied"]


def test_llm_error_is_returned_not_raised(monkeypatch, _fake_client):
    def boom(*a, **k):
        raise RuntimeError("429")

    patch_mini(monkeypatch, boom)
    out = mini_agent.run_tool_loop(
        system_prompt="s", user_content="u", allowed_tools=["read_file"]
    )
    assert out["text"] == ""
    assert "429" in out["error"]


def test_malformed_args_do_not_crash(monkeypatch, _fake_client):
    monkeypatch.setattr(mini_agent, "execute_tool", lambda n, a: "ok")
    responses = iter([
        _resp(_msg(tool_calls=[_tool_call("1", "read_file", "不是 JSON")])),
        _resp(_msg(content="done")),
    ])
    patch_mini(monkeypatch, lambda *a, **k: next(responses))
    out = mini_agent.run_tool_loop(
        system_prompt="s", user_content="u", allowed_tools=["read_file"], max_steps=2
    )
    assert out["text"] == "done"


def test_empty_allowed_tools(monkeypatch, _fake_client):
    out = mini_agent.run_tool_loop(system_prompt="s", user_content="u", allowed_tools=[])
    assert out["error"] == "没有可用工具"


def test_max_steps_is_respected(monkeypatch, _fake_client):
    monkeypatch.setattr(mini_agent, "execute_tool", lambda n, a: "ok")
    patch_mini(monkeypatch, lambda *a, **k: _resp(_msg(tool_calls=[_tool_call("1", "read_file")])),
    )
    out = mini_agent.run_tool_loop(
        system_prompt="s", user_content="u", allowed_tools=["read_file"], max_steps=3
    )
    assert out["steps"] == 3
    assert "最大步数" in out["error"]


def test_planner_tools_are_readonly():
    """Planner 的工具集必须是纯只读 —— 它和 Executor 的边界就在这里。"""
    import tools

    for name in mini_agent.READONLY_TOOLS:
        assert tools.tool_permission_kind(name) == "read", f"{name} 不是只读工具"


# ============ Planner ============


def test_planner_returns_steps_and_reports_exploration(monkeypatch):
    calls = []

    def fake_loop(**kwargs):
        calls.append(kwargs)
        return {"text": json.dumps({"steps": ["改 a.py", "跑测试"]}),
                "tool_calls": [{"name": "read_file", "args": {}}], "error": None}

    monkeypatch.setattr(planner.mini_agent, "run_tool_loop", fake_loop)
    plan = planner.create_plan("修 bug", project_root="/tmp/p")
    assert plan["steps"] == ["改 a.py", "跑测试"]
    assert plan["explored"] == 1
    # 必须把 project_root 告诉 Planner，否则它不知道去哪探索
    assert calls[0]["user_content"].count("/tmp/p") == 1
    assert set(calls[0]["allowed_tools"]) == set(mini_agent.READONLY_TOOLS)


def test_planner_parses_fenced_json(monkeypatch):
    monkeypatch.setattr(
        planner.mini_agent, "run_tool_loop",
        lambda **k: {"text": '```json\n{"steps": ["一步"]}\n```', "tool_calls": []},
    )
    assert planner.create_plan("t")["steps"] == ["一步"]


def test_planner_degrades_on_error(monkeypatch):
    monkeypatch.setattr(
        planner.mini_agent, "run_tool_loop",
        lambda **k: {"text": "", "tool_calls": [], "error": "quota exceeded"},
    )
    plan = planner.create_plan("修 bug")
    assert plan["steps"] == ["修 bug"], "失败必须降级为单步计划，而不是让任务挂掉"
    assert "error" in plan


def test_planner_degrades_on_garbage(monkeypatch):
    monkeypatch.setattr(
        planner.mini_agent, "run_tool_loop",
        lambda **k: {"text": "我觉得应该先看看代码", "tool_calls": []},
    )
    plan = planner.create_plan("t")
    assert plan["steps"] == ["t"]
    assert "error" in plan


def test_planner_normalizes_string_steps(monkeypatch):
    monkeypatch.setattr(
        planner.mini_agent, "run_tool_loop",
        lambda **k: {"text": json.dumps({"steps": "只有一步"}), "tool_calls": []},
    )
    assert planner.create_plan("t")["steps"] == ["只有一步"]


# ============ Reviewer ============


def test_reviewer_parses_verdict(monkeypatch):
    monkeypatch.setattr(runtime, "get_permission_level", lambda sid=None: 2)
    monkeypatch.setattr(
        reviewer.mini_agent, "run_tool_loop",
        lambda **k: {
            "text": json.dumps({"passed": True, "reason": "读了代码，没问题",
                                "suggestions": ["加个测试"]}),
            "tool_calls": [{"name": "read_file", "args": {}}],
        },
    )
    out = reviewer.review("任务", "改好了")
    assert out["passed"] is True
    assert out["suggestions"] == ["加个测试"]
    assert out["tests_run"] is False, "没跑测试就必须如实报告"


def test_reviewer_reports_tests_run(monkeypatch):
    monkeypatch.setattr(runtime, "get_permission_level", lambda sid=None: 2)
    policy.set_policy({"allow_reviewer_tests": True})
    monkeypatch.setattr(
        reviewer.mini_agent, "run_tool_loop",
        lambda **k: {
            "text": json.dumps({"passed": True, "reason": "测试通过", "suggestions": []}),
            "tool_calls": [{"name": "run_tests", "args": {}}],
        },
    )
    assert reviewer.review("任务", "改好了")["tests_run"] is True


def test_reviewer_gets_run_tests_only_when_allowed(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        reviewer.mini_agent, "run_tool_loop",
        lambda **k: seen.update(k) or {"text": "{}", "tool_calls": []},
    )
    monkeypatch.setattr(runtime, "get_permission_level", lambda sid=None: 1)
    reviewer.review("t", "r")
    assert "run_tests" not in seen["allowed_tools"], "等级 1 绝不能给 run_tests"

    monkeypatch.setattr(runtime, "get_permission_level", lambda sid=None: 2)
    policy.set_policy({"allow_reviewer_tests": True})
    reviewer.review("t", "r")
    assert "run_tests" in seen["allowed_tools"]

    policy.set_policy({"allow_reviewer_tests": False})
    reviewer.review("t", "r")
    assert "run_tests" not in seen["allowed_tools"], "开关关掉后必须回到静态审查"


def test_reviewer_prompt_says_not_to_trust_executor(monkeypatch):
    """提示词必须明确：executor 的自述未经核实。"""
    seen = {}
    monkeypatch.setattr(
        reviewer.mini_agent, "run_tool_loop",
        lambda **k: seen.update(k) or {"text": "{}", "tool_calls": []},
    )
    monkeypatch.setattr(runtime, "get_permission_level", lambda sid=None: 1)
    reviewer.review("任务", "我全改好了，测试通过")
    assert "未经核实" in seen["user_content"]
    assert "未运行测试" in seen["user_content"]


def test_reviewer_passed_is_none_when_unverifiable(monkeypatch):
    monkeypatch.setattr(runtime, "get_permission_level", lambda sid=None: 1)
    monkeypatch.setattr(
        reviewer.mini_agent, "run_tool_loop",
        lambda **k: {"text": "无法判断", "tool_calls": []},
    )
    assert reviewer.review("t", "r")["passed"] is None


def test_reviewer_non_bool_passed_becomes_none(monkeypatch):
    monkeypatch.setattr(runtime, "get_permission_level", lambda sid=None: 1)
    monkeypatch.setattr(
        reviewer.mini_agent, "run_tool_loop",
        lambda **k: {"text": json.dumps({"passed": "yes", "reason": "x"}), "tool_calls": []},
    )
    assert reviewer.review("t", "r")["passed"] is None, "字符串 'yes' 不能当成 True"


def test_reviewer_project_root_is_passed(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        reviewer.mini_agent, "run_tool_loop",
        lambda **k: seen.update(k) or {"text": "{}", "tool_calls": []},
    )
    monkeypatch.setattr(runtime, "get_permission_level", lambda sid=None: 1)
    reviewer.review("t", "r", project_root="/tmp/proj")
    assert "/tmp/proj" in seen["user_content"]
