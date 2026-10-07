"""运行时状态落库（跨重启恢复）的回归测试。

背景：pending_state / plan / project_root 原先只存在内存里，服务一重启就丢——
用户点「确认」会提示找不到挂起状态，Planner 的计划也凭空消失。
这里验证「模拟重启后仍能恢复」，以及 TTL、清理、降级等边界。
"""

import json
import os
import shutil
import tempfile
import time
import types

import pytest

import agent
import db
import runtime
import tools

HOME = os.path.expanduser("~")


@pytest.fixture(autouse=True)
def _clean():
    runtime.reset_all()
    runtime.set_session_id(None)
    yield
    runtime.reset_all()
    runtime.set_session_id(None)


def _simulate_restart():
    """只清内存缓存，保留 SQLite 里的数据 = 服务重启。"""
    runtime._project_root_store.clear_memory()
    runtime._pending_store.clear_memory()
    runtime._pending_text_store.clear_memory()
    tools._plan_store.clear_memory()
    runtime.set_session_id(None)


# ============ 基础：写进去 → 重启 → 读回来 ============


def test_pending_state_survives_restart():
    runtime.set_pending_state({"task": "改配置", "step": 3, "confirm_token": "tk"}, "s1")

    _simulate_restart()

    restored = runtime.get_pending_state("s1")
    assert restored is not None, "重启后挂起状态丢失，用户点确认会失败"
    assert restored["task"] == "改配置"
    assert restored["step"] == 3
    assert restored["confirm_token"] == "tk"


def test_plan_survives_restart():
    runtime.set_session_id("s1")
    tools.create_plan(["第一步", "第二步"])
    tools.update_plan(1, "done")

    _simulate_restart()

    runtime.set_session_id("s1")
    plan = tools.get_plan_data()
    assert [p["step"] for p in plan] == ["第一步", "第二步"]
    assert plan[0]["status"] == "done", "重启后步骤状态丢失"


def test_project_root_survives_restart():
    runtime.set_project_root("/tmp/proj-x", "s1")
    _simulate_restart()
    assert runtime.get_project_root("s1") == "/tmp/proj-x"


def test_pending_text_survives_restart():
    runtime.set_pending_text("已输出的前半段", "s1")
    _simulate_restart()
    assert runtime.get_pending_text("s1") == "已输出的前半段"


def test_sessions_stay_isolated_across_restart():
    runtime.set_pending_state({"task": "A"}, "sA")
    runtime.set_pending_state({"task": "B"}, "sB")

    _simulate_restart()

    assert runtime.get_pending_state("sA")["task"] == "A"
    assert runtime.get_pending_state("sB")["task"] == "B"


# ============ 删除不能「复活」 ============


def test_clear_pending_state_removes_from_db_too():
    runtime.set_pending_state({"task": "x"}, "s1")
    runtime.clear_pending_state("s1")

    _simulate_restart()

    assert runtime.get_pending_state("s1") is None, "删除后又被库里的旧值复活了"
    assert db.get_runtime_state("s1", "pending") is None


def test_clear_session_wipes_all_kinds():
    runtime.set_session_id("s1")
    runtime.set_project_root("/tmp/p", "s1")
    runtime.set_pending_state({"task": "t"}, "s1")
    runtime.set_pending_text("txt", "s1")
    tools.create_plan(["步骤"])

    runtime.clear_session("s1")

    _simulate_restart()

    assert runtime.get_pending_state("s1") is None
    assert runtime.get_pending_text("s1") == ""
    assert runtime.get_project_root("s1") != "/tmp/p"
    runtime.set_session_id("s1")
    assert tools.get_plan_data() == []


# ============ TTL ============


def test_expired_pending_state_is_ignored_and_cleaned():
    """过期的挂起确认不应被恢复（避免重启后翻出一周前的旧确认让用户误点）。"""
    old = time.time() - 48 * 3600  # 48 小时前
    db.upsert_runtime_state("s-old", "pending", json.dumps({"task": "old"}), old)

    _simulate_restart()

    assert runtime.get_pending_state("s-old") is None
    assert db.get_runtime_state("s-old", "pending") is None, "过期记录应被顺手清理"


def test_fresh_pending_state_within_ttl_is_restored():
    db.upsert_runtime_state(
        "s-fresh", "pending", json.dumps({"task": "fresh"}), time.time() - 60
    )
    _simulate_restart()
    assert runtime.get_pending_state("s-fresh")["task"] == "fresh"


# ============ 降级：库不可用不能影响主流程 ============


def test_write_degrades_gracefully_when_db_fails(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "upsert_runtime_state", boom)
    # 不应抛异常
    runtime.set_pending_state({"task": "t"}, "s1")
    assert runtime.get_pending_state("s1")["task"] == "t"


def test_read_degrades_gracefully_when_db_fails(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "get_runtime_state", boom)
    _simulate_restart()
    assert runtime.get_pending_state("s-unknown") is None


# ============ 端到端：重启后确认令牌仍然有效并能续跑 ============


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


@pytest.fixture
def project(monkeypatch):
    d = tempfile.mkdtemp(prefix=".agentcode-persist-", dir=HOME)
    monkeypatch.setattr(tools, "TRASH_DIR", os.path.join(d, ".trash"))
    monkeypatch.setattr(agent, "get_llm_client", lambda *a, **k: object())
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_confirm_and_resume_still_works_after_restart(project, monkeypatch):
    """核心场景：用户看到确认框 → 服务重启 → 点确认仍能正确续跑。"""
    target = os.path.join(project, "cfg.txt")
    with open(target, "w", encoding="utf-8") as f:
        f.write("OLD")

    monkeypatch.setattr(
        agent,
        "call_llm_with_retry",
        _llm(
            [
                _Chunk(
                    _Delta(
                        tool_calls=[
                            _TC(
                                0,
                                id="c1",
                                name="write_file",
                                arguments=json.dumps({"path": target, "content": "NEW"}),
                            )
                        ]
                    )
                )
            ]
        ),
    )
    runtime.set_session_id("persist-sess")
    runtime.set_project_root(project)

    events = list(
        agent.run_agent_stream("改配置", session_id="persist-sess", user_api_key="k")
    )
    confirm = next(e for e in events if e["type"] == "confirm_write")
    token = confirm["confirm_token"]
    assert open(target, encoding="utf-8").read() == "OLD"

    # ---- 模拟服务重启 ----
    _simulate_restart()
    runtime.set_session_id("persist-sess")

    # 重启后令牌仍然可用（状态已落库）
    assert runtime.verify_and_consume_confirm_token("persist-sess", token) is True

    # 再重启一次：已消费的令牌不能复活
    _simulate_restart()
    assert runtime.verify_and_consume_confirm_token("persist-sess", token) is False
