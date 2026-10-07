"""P2 会话隔离的回归测试。

核心保证：并发会话之间，计划 / 待确认状态 / 项目根目录互不干扰
（这是「多会话并行」这条能力成立的前提）。
"""

import threading
import time

import pytest

import runtime
import tools
from utils import get_llm_client


@pytest.fixture(autouse=True)
def _clean_state():
    runtime.reset_all()
    runtime.set_session_id(None)
    for sid in ("sessA", "sessB"):
        tools.clear_plan(sid)
    yield
    runtime.reset_all()
    runtime.set_session_id(None)
    for sid in ("sessA", "sessB"):
        tools.clear_plan(sid)


# ============ 计划隔离 ============


def test_plan_is_isolated_per_session():
    runtime.set_session_id("sessA")
    tools.create_plan(["A-1", "A-2"])

    runtime.set_session_id("sessB")
    tools.create_plan(["B-1"])

    runtime.set_session_id("sessA")
    assert "A-1" in tools.get_plan()
    assert "A-2" in tools.get_plan()
    assert "B-1" not in tools.get_plan()

    runtime.set_session_id("sessB")
    assert "B-1" in tools.get_plan()
    assert "A-1" not in tools.get_plan()


def test_plan_update_does_not_leak_across_sessions():
    runtime.set_session_id("sessA")
    tools.create_plan(["A-1", "A-2"])
    tools.update_plan(1, "done")

    runtime.set_session_id("sessB")
    tools.create_plan(["B-1", "B-2"])
    # B 的第 1 步不应被 A 的更新影响
    assert tools.get_plan_data()[0]["status"] == "pending"

    runtime.set_session_id("sessA")
    assert tools.get_plan_data()[0]["status"] == "done"


def test_clear_plan_only_affects_target_session():
    runtime.set_session_id("sessA")
    tools.create_plan(["A-1"])
    runtime.set_session_id("sessB")
    tools.create_plan(["B-1"])

    tools.clear_plan("sessA")

    runtime.set_session_id("sessA")
    assert tools.get_plan() == "（当前无计划）"
    runtime.set_session_id("sessB")
    assert "B-1" in tools.get_plan()


# ============ 待确认状态隔离（断点续跑的关键）============


def test_pending_state_is_isolated_per_session():
    runtime.set_session_id("sessA")
    runtime.set_pending_state({"task": "A 任务", "step": 3})

    runtime.set_session_id("sessB")
    runtime.set_pending_state({"task": "B 任务", "step": 7})

    # 两个会话各自挂起的确认互不覆盖
    assert runtime.get_pending_state()["task"] == "B 任务"
    runtime.set_session_id("sessA")
    assert runtime.get_pending_state()["task"] == "A 任务"
    assert runtime.get_pending_state()["step"] == 3


def test_clear_pending_state_is_scoped():
    runtime.set_session_id("sessA")
    runtime.set_pending_state({"task": "A"})
    runtime.set_session_id("sessB")
    runtime.set_pending_state({"task": "B"})

    runtime.set_session_id("sessA")
    runtime.clear_pending_state()
    assert runtime.get_pending_state() is None

    runtime.set_session_id("sessB")
    assert runtime.get_pending_state()["task"] == "B"


def test_pending_text_is_isolated_per_session():
    runtime.set_session_id("sessA")
    runtime.set_pending_text("A 已输出")
    runtime.set_session_id("sessB")
    runtime.set_pending_text("B 已输出")

    runtime.set_session_id("sessA")
    assert runtime.get_pending_text() == "A 已输出"
    runtime.set_session_id("sessB")
    assert runtime.get_pending_text() == "B 已输出"


# ============ 项目根目录隔离 ============


def test_project_root_is_isolated_per_session():
    runtime.set_session_id("sessA")
    runtime.set_project_root("/tmp/projA")
    runtime.set_session_id("sessB")
    runtime.set_project_root("/tmp/projB")

    runtime.set_session_id("sessA")
    assert runtime.get_project_root() == "/tmp/projA"
    runtime.set_session_id("sessB")
    assert runtime.get_project_root() == "/tmp/projB"


def test_clear_session_removes_all_sticky_state():
    runtime.set_session_id("sessA")
    runtime.set_project_root("/tmp/projA")
    runtime.set_pending_state({"task": "A"})
    runtime.set_pending_text("A")
    tools.create_plan(["A-1"])

    runtime.clear_session("sessA")
    tools.clear_plan("sessA")

    runtime.set_session_id("sessA")
    assert runtime.get_pending_state() is None
    assert runtime.get_pending_text() == ""
    assert runtime.get_project_root() != "/tmp/projA"
    assert tools.get_plan() == "（当前无计划）"


# ============ 默认桶兜底（CLI / 脚本调用）============


def test_default_bucket_when_no_session_context():
    runtime.set_session_id(None)
    assert runtime.session_key() == runtime.DEFAULT_SESSION_KEY

    runtime.set_project_root("/tmp/noSession")
    assert runtime.get_project_root() == "/tmp/noSession"
    assert runtime.session_key() == runtime.DEFAULT_SESSION_KEY


def test_plan_works_without_session_context():
    runtime.set_session_id(None)
    tools.clear_plan(None)
    assert "已创建计划" in tools.create_plan(["无会话步骤"])
    assert "无会话步骤" in tools.get_plan()
    tools.clear_plan(None)


# ============ 真实并发（线程级）============


def test_concurrent_threads_do_not_share_state():
    """两个请求线程各跑一个会话，交替写入计划与 pending，结果必须互不污染。

    这是「多会话并行」的实际场景：session_id 走 ContextVar，
    每个线程有独立上下文，因此 A 的确认挂起不会被 B 冲掉。
    """
    results = {}
    errors = []
    barrier = threading.Barrier(2)

    def worker(sid, label):
        try:
            runtime.set_session_id(sid)
            barrier.wait()  # 强制两个线程真正交错执行
            tools.create_plan([f"{sid}-1", f"{sid}-2"])
            runtime.set_project_root(f"/tmp/{sid}")
            runtime.set_pending_state({"task": label, "step": 1})
            time.sleep(0.05)
            runtime.set_pending_state({"task": label, "step": 2})
            results[sid] = {
                "plan": tools.get_plan_data(),
                "pending": runtime.get_pending_state(),
                "root": runtime.get_project_root(),
            }
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [
        threading.Thread(target=worker, args=("sessA", "A 任务")),
        threading.Thread(target=worker, args=("sessB", "B 任务")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"并发执行出现异常：{errors}"
    assert set(results) == {"sessA", "sessB"}

    a, b = results["sessA"], results["sessB"]
    assert [s["step"] for s in a["plan"]] == ["sessA-1", "sessA-2"]
    assert [s["step"] for s in b["plan"]] == ["sessB-1", "sessB-2"]
    assert a["pending"]["task"] == "A 任务"
    assert b["pending"]["task"] == "B 任务"
    assert a["root"] == "/tmp/sessA"
    assert b["root"] == "/tmp/sessB"


# ============ LLM 客户端复用 ============


def test_llm_client_is_reused_per_key_and_base_url():
    c1 = get_llm_client("sk-test-1", "https://api.deepseek.com/v1")
    c2 = get_llm_client("sk-test-1", "https://api.deepseek.com/v1")
    c3 = get_llm_client("sk-test-2", "https://api.deepseek.com/v1")
    c4 = get_llm_client("sk-test-1", "https://api.openai.com/v1")

    assert c1 is c2, "相同 key/url 应复用同一客户端实例"
    assert c1 is not c3, "不同 key 应使用不同客户端"
    assert c1 is not c4, "不同 base_url 应使用不同客户端"
