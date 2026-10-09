"""评估装置的测试。

**为什么必须测这个**：评估集跑一次要花真金白银和时间，如果装置本身是坏的
（前置检查没生效、验收命令写错、指标收不到），会给出**看起来很专业但完全错误**的结论。
那比没有评估更糟。

所以这里用**假 LLM** 把装置本身跑通 —— 免费、可在 CI 里跑。
真实的模型效果评估用 ``python eval/run.py`` 手动跑（不进 CI）。
"""

import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_ROOT, "src"))
sys.path.insert(0, os.path.join(_ROOT, "eval"))

import harness  # noqa: E402
import tasks as eval_tasks  # noqa: E402


@pytest.fixture
def tiny_task():
    """一个最小任务：把 broken.py 里的 1+1 改成 2。"""
    return eval_tasks.Task(
        id="tiny",
        title="最小任务",
        prompt="让测试通过",
        files={
            "broken.py": "VALUE = 1 + 1 - 1\n",
            "check.py": "from broken import VALUE\n\nassert VALUE == 2, VALUE\nprint('ok')\n",
        },
        # 用 python3 而不是 python：本机只有 python3，agent-sandbox 里也是 python3
        check_cmd="python3 check.py",
        tags=["tiny"],
    )


def _fake_agent(monkeypatch, work, *, metrics=None, crash=False):
    """把 agent.run_agent_stream 换成一个「直接改文件」的假实现。"""
    import agent

    def fake(prompt, **kw):
        if crash:
            raise RuntimeError("假实现故意崩了")
        with open(os.path.join(kw["project_root"], "broken.py"), "w", encoding="utf-8") as f:
            f.write(work)
        yield {"type": "tool_call", "name": "write_file", "args": {"path": "broken.py"}}
        # 指标挂在 token_usage 上（done 事件不带指标）—— 这里刻意用真实的事件名
        yield {"type": "token_usage", **(metrics or {"steps": 2, "prompt_tokens": 100,
                                                     "completion_tokens": 50})}
        yield {"type": "done"}

    monkeypatch.setattr(agent, "run_agent_stream", fake)


# ============ 装置本身 ============


def test_env_is_isolated():
    """评估必须有自己的 DB 与空的 MCP 配置，不能碰真实数据。"""
    d = harness.isolate_env()
    assert harness._EVAL_TMP == d
    assert "agentcode-eval-" in os.environ["AGENT_DB_PATH"]
    import json
    with open(os.environ["AGENT_MCP_PATH"], encoding="utf-8") as f:
        assert json.load(f)["servers"] == {}


def test_prepare_repo_creates_git_repo(tmp_path):
    t = eval_tasks.Task(id="x", title="x", prompt="x",
                        files={"a.txt": "hi"}, check_cmd="true")
    wd = harness.prepare_repo(t, str(tmp_path))
    assert os.path.exists(os.path.join(wd, "a.txt"))
    assert os.path.isdir(os.path.join(wd, ".git")), "必须是 git 仓库（git_* 工具要用）"


def test_check_task_reports_failure(tmp_path):
    t = eval_tasks.Task(id="x", title="x", prompt="x", files={},
                        check_cmd="python3 -c \"raise SystemExit(3)\"")
    ok, msg = harness.check_task(t, str(tmp_path))
    assert not ok and "退出码 3" in msg


def test_check_task_extra_check(tmp_path):
    t = eval_tasks.Task(id="x", title="x", prompt="x", files={},
                        check_cmd="true", extra_check="false")
    ok, msg = harness.check_task(t, str(tmp_path))
    assert not ok and "额外检查" in msg


# ============ 跑通一个任务 ============


def test_run_task_success(tiny_task, monkeypatch):
    _fake_agent(monkeypatch, "VALUE = 1 + 1\n")
    r = harness.run_task(tiny_task)
    assert r["passed"] is True, r["reason"]
    assert r["steps"] == 2
    assert r["prompt_tokens"] == 100
    assert r["tool_calls"] == 1
    assert r["duration_s"] >= 0


def test_run_task_reports_failure(tiny_task, monkeypatch):
    """模型没干活时必须如实报失败 —— 这是评估集的价值所在。"""
    _fake_agent(monkeypatch, "VALUE = 1 + 1 - 1\n")      # 没改对
    r = harness.run_task(tiny_task)
    assert r["passed"] is False
    assert "验收失败" in r["reason"]


def test_run_task_handles_agent_crash(tiny_task, monkeypatch):
    _fake_agent(monkeypatch, "", crash=True)
    r = harness.run_task(tiny_task)
    assert r["passed"] is False
    assert "故意崩了" in r["error"]


def test_precheck_guards_against_already_passing_task(monkeypatch):
    """**关键防线**：任务本来就通过时，必须拒绝给分。

    否则验收脚本写错（比如命令拼错、永远返回 0）会让通过率虚高，
    而这种错误从报告上完全看不出来。
    """
    t = eval_tasks.Task(id="already", title="already", prompt="x",
                        files={"ok.py": "V=1\n"},
                        check_cmd="python3 -c \"print('ok')\"")   # 永远成功
    _fake_agent(monkeypatch, "V=1\n")
    r = harness.run_task(t)
    assert r["passed"] is False
    assert "前置检查未失败" in r["reason"]


def test_compaction_is_counted(tiny_task, monkeypatch):
    import agent

    def fake(prompt, **kw):
        with open(os.path.join(kw["project_root"], "broken.py"), "w", encoding="utf-8") as f:
            f.write("VALUE = 1 + 1\n")
        yield {"type": "context_stats", "before_tokens": 40000, "after_tokens": 20000,
               "budget": 32000, "pruned": 1, "folded": 2, "saved_chars": 5000}
        yield {"type": "token_usage", "steps": 9, "prompt_tokens": 1, "completion_tokens": 1}
        yield {"type": "done"}

    monkeypatch.setattr(agent, "run_agent_stream", fake)
    r = harness.run_task(tiny_task)
    assert r["compactions"] == 1
    assert r["passed"] is True


# ============ 汇总与对比 ============


def test_summarize():
    results = [
        {"passed": True, "steps": 4, "prompt_tokens": 100, "completion_tokens": 50,
         "tool_calls": 2, "duration_s": 10.0, "compactions": 0},
        {"passed": False, "steps": 6, "prompt_tokens": 200, "completion_tokens": 60,
         "tool_calls": 3, "duration_s": 20.0, "compactions": 1},
    ]
    s = harness.summarize(results)
    assert s["tasks"] == 2 and s["passed"] == 1
    assert s["pass_rate"] == 0.5
    assert s["avg_steps"] == 5.0
    assert s["total_tokens"] == 410
    assert s["compactions"] == 1


def test_summarize_handles_empty():
    s = harness.summarize([])
    assert s["tasks"] == 0 and s["pass_rate"] == 0.0


# ============ 任务集自身的健全性 ============


def test_all_tasks_are_well_formed():
    assert eval_tasks.ALL_TASKS, "任务集不能为空"
    for t in eval_tasks.ALL_TASKS:
        assert t.id and t.prompt and t.check_cmd
        assert t.files, f"{t.id} 没有初始文件"
        assert t.tags, f"{t.id} 没有标签（无法按标签筛选）"


def test_task_ids_are_unique():
    ids = [t.id for t in eval_tasks.ALL_TASKS]
    assert len(ids) == len(set(ids))


def test_every_task_actually_starts_broken():
    """**最重要的一条**：每条任务的初始状态都必须真的失败。

    如果某条任务一开始就是通过的，它在评估里永远是 ✅，
    既没有信息量，还会掩盖「验收命令写错了」。
    """
    import tempfile
    d = tempfile.mkdtemp(prefix="evalcheck-", dir=os.path.expanduser("~"))
    try:
        broken = []
        for t in eval_tasks.ALL_TASKS:
            wd = harness.prepare_repo(t, d)
            ok, _msg = harness.check_task(t, wd)
            if ok:
                broken.append(t.id)
        assert not broken, f"这些任务一开始就是通过的（验收或初始状态有问题）：{broken}"
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def test_select_by_tag_and_id():
    assert eval_tasks.select(["fix_failing_test"])[0].id == "fix_failing_test"
    assert all("bugfix" in t.tags for t in eval_tasks.select(tags=["bugfix"]))
    assert eval_tasks.select(["nope"]) == []
