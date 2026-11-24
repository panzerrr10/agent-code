
"""「第 N 轮改动」的测试。

这个功能的正确性有三个层面，全部要测：

1. **记得住**：写/改/删都产生记录，同文件多次改要合并
2. **分得清**：不同轮次互不干扰；**续跑属于同一轮**（这是用户明确要求的）
3. **存得准**：diff 存的是**当时**的改动，用户之后又改文件也不会让历史失真

还有一条容易被忽略：**拿不到会话/轮次时必须静默跳过** ——
CLI 单次运行、评估集里没有「轮次」这个概念，不能因此让工具报错。
"""

import json
import os
import shutil
import sys
import tempfile

import pytest

from tests.fakes import patch_agent

sys.path.insert(0, "src")

import db  # noqa: E402
import runtime  # noqa: E402
import tools  # noqa: E402


@pytest.fixture
def proj():
    """HOME 下的临时工作区（路径白名单只放行主目录）。"""
    d = tempfile.mkdtemp(prefix="agentcode-rounds-", dir=os.path.expanduser("~"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def session(client, proj):
    ws = client.post("/workspaces", json={"name": "w", "path": proj}).get_json()
    s = client.post("/sessions", json={"workspace_id": ws["id"]}).get_json()
    sid = s["id"]
    runtime.set_session_id(sid)
    runtime.set_run_index(1)
    return sid


def _mk(proj, name, text):
    p = os.path.join(proj, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


# ============ 记录 ============


def test_create_is_recorded(session, proj):
    tools.create_file(os.path.join(proj, "new.py"), "a\nb\n")
    rows = db.get_run_changes(session, 1)
    assert len(rows) == 1
    assert rows[0]["change_type"] == "create"
    assert rows[0]["added"] == 2


def test_write_is_recorded_with_diff(session, proj):
    p = _mk(proj, "a.py", "x = 1\ny = 2\n")
    tools.write_file(p, "x = 1\ny = 99\n")
    d = db.get_run_change_diff(session, 1, p)
    assert d is not None
    assert "-y = 2" in d["diff"] and "+y = 99" in d["diff"]
    assert d["added"] == 1 and d["removed"] == 1


def test_edit_is_recorded(session, proj):
    p = _mk(proj, "a.py", "x = 1\n")
    tools.edit_file(p, "x = 1", "x = 2")
    d = db.get_run_change_diff(session, 1, p)
    assert "-x = 1" in d["diff"] and "+x = 2" in d["diff"]


def test_delete_is_recorded_with_content(session, proj):
    """用户明确要求：**列出删了什么**。"""
    p = _mk(proj, "gone.py", "bye\n")
    tools.delete_file(p)
    d = db.get_run_change_diff(session, 1, p)
    assert d["change_type"] == "delete"
    assert "-bye" in d["diff"], "删除也要能看出删掉了什么内容"


def test_multi_edit_is_recorded(session, proj):
    p = _mk(proj, "a.py", "x = 1\ny = 2\n")
    tools.multi_edit([
        {"path": p, "old_text": "x = 1", "new_text": "x = 10"},
        {"path": p, "old_text": "y = 2", "new_text": "y = 20"},
    ])
    d = db.get_run_change_diff(session, 1, p)
    assert "+x = 10" in d["diff"] and "+y = 20" in d["diff"]


def test_same_file_twice_is_merged(session, proj):
    """同一轮里改同一个文件多次 → **一条记录**，不是流水账。"""
    p = _mk(proj, "a.py", "x = 1\n")
    tools.write_file(p, "x = 2\n")
    tools.edit_file(p, "x = 2", "x = 3")
    rows = db.get_run_changes(session, 1)
    assert len(rows) == 1, "同文件应合并成一条"
    assert rows[0]["added"] == 2 and rows[0]["removed"] == 2
    d = db.get_run_change_diff(session, 1, p)
    assert "+x = 2" in d["diff"] and "+x = 3" in d["diff"], "两次改动都要在 diff 里"


def test_created_then_modified_keeps_create(session, proj):
    """本轮新建的文件后面又被改，仍应显示「新建」—— 否则用户不知道它是新出现的。"""
    p = os.path.join(proj, "n.py")
    tools.create_file(p, "a\n")
    tools.edit_file(p, "a", "b")
    assert db.get_run_changes(session, 1)[0]["change_type"] == "create"


def test_created_then_deleted_is_delete(session, proj):
    """新建又删掉 —— 「删了」永远优先，那是最重要的信息。"""
    p = os.path.join(proj, "n.py")
    tools.create_file(p, "a\n")
    tools.delete_file(p)
    assert db.get_run_changes(session, 1)[0]["change_type"] == "delete"


def test_failed_operations_are_not_recorded(session, proj):
    """失败的操作不该产生记录 —— 否则「本轮改了什么」会列出根本没改成的文件。"""
    missing = os.path.join(proj, "nope_missing.py")
    out = tools.write_file(missing, "x")            # 文件不存在 → 工具返回失败
    assert out.startswith("❌"), out
    left = [c for c in db.get_run_changes(session, 1) if c["path"] == missing]
    assert not left, "失败的操作不该留下改动记录"


def test_no_session_means_no_recording(client, proj):
    """**CLI / 评估集里没有「轮次」概念，必须静默跳过而不是报错。**"""
    runtime.set_session_id(None)
    runtime.set_run_index(None)
    p = _mk(proj, "t_nosession.py", "x = 1\n")
    out = tools.write_file(p, "x = 2\n")
    assert out.startswith("✅"), out          # 工具本身照常工作
    # 针对**这个文件**断言：整个库里可能有别的测试留下的记录，不能一刀切
    assert not any(c["path"] == p for r in (1, 2, 3)
                   for c in db.get_run_changes(r, 1))


def test_no_run_index_means_no_recording(session, proj):
    runtime.set_run_index(None)
    p = _mk(proj, "a.py", "x = 1\n")
    assert tools.write_file(p, "x = 2\n").startswith("✅")
    assert db.get_run_changes(session, 1) == []


# ============ 轮次 ============


def test_rounds_are_isolated(session, proj):
    p = _mk(proj, "a.py", "x = 1\n")
    tools.write_file(p, "x = 2\n")
    runtime.set_run_index(2)
    tools.write_file(p, "x = 3\n")
    assert len(db.get_run_changes(session, 1)) == 1
    assert len(db.get_run_changes(session, 2)) == 1
    assert "+x = 2" in db.get_run_change_diff(session, 1, p)["diff"]
    assert "+x = 3" in db.get_run_change_diff(session, 2, p)["diff"]


def test_history_lists_rounds_desc(session, proj):
    p = _mk(proj, "a.py", "x = 1\n")
    tools.write_file(p, "x = 2\n")
    runtime.set_run_index(2)
    tools.write_file(p, "x = 3\n")
    rounds = db.get_changed_run_indexes(session)
    assert [r["run_index"] for r in rounds] == [2, 1], "历史应倒序（最近的在前）"
    assert all(r["files"] >= 1 for r in rounds)


def test_diff_is_snapshot_not_current_disk(session, proj):
    """**存的是当轮快照。** 用户之后又手动改了文件，第 1 轮的 diff 不能跟着变。"""
    p = _mk(proj, "a.py", "x = 1\n")
    tools.write_file(p, "x = 2\n")
    before = db.get_run_change_diff(session, 1, p)["diff"]
    # 模拟用户之后自己又改了（没有经过工具，所以不产生记录）
    with open(p, "w", encoding="utf-8") as f:
        f.write("x = 999\n")
    assert db.get_run_change_diff(session, 1, p)["diff"] == before, \
        "第 1 轮的 diff 必须保持当时的快照"


def test_run_index_comes_from_user_message_count(client, proj):
    """「第几轮」= 该会话已有几条用户消息。"""
    ws = client.post("/workspaces", json={"name": "w2", "path": proj}).get_json()
    sid = client.post("/sessions", json={"workspace_id": ws["id"]}).get_json()["id"]
    assert db.count_user_messages(sid) == 0
    db.save_message(sid, "user", "第一轮")
    db.save_message(sid, "assistant", "好")
    assert db.count_user_messages(sid) == 1
    db.save_message(sid, "user", "第二轮")
    assert db.count_user_messages(sid) == 2


def test_resume_keeps_same_round(monkeypatch, session, proj):
    """**续跑属于同一轮**（用户明确要求）—— pending 里要带着 run_index。"""
    import types

    import agent

    runtime.set_run_index(7)
    seen = {}

    class _D:
        def __init__(s, c=None, tc=None):
            s.content = c
            s.tool_calls = tc
            s.reasoning_content = None

    class _K:
        def __init__(s, d):
            s.choices = [type("C", (), {"delta": d})()]
            s.usage = None

    class _TC:
        def __init__(s, i, id_, n, a):
            s.index = i
            s.id = id_
            s.function = types.SimpleNamespace(name=n, arguments=a)

    calls = {"n": 0}

    def fake_llm(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            # 第一次：发起一个需要确认的写操作（会存 pending 并 return）
            target = os.path.join(proj, "w.py")
            tc = _TC(0, "c1", "write_file",
                     json.dumps({"path": target, "content": "new\\n"}))
            return iter([_K(_D(tc=[tc]))])
        seen["messages"] = k.get("messages") or []
        return iter([_K(_D(c="好"))])

    patch_agent(monkeypatch, fake_llm)

    list(agent.run_agent_stream("改个文件", session_id=session, user_api_key="k"))
    state = runtime.get_pending_state(session)
    assert state is not None, "应当停在确认点"
    assert state.get("run_index") == 7, "pending 里必须带上轮次，续跑才知道属于同一轮"


# ============ 接口 ============


def test_api_list(session, proj):
    p = _mk(proj, "a.py", "x = 1\n")
    tools.write_file(p, "x = 2\n")
    import app as app_module
    c = app_module.app.test_client()
    import auth
    c.environ_base["HTTP_X_AUTH_TOKEN"] = auth.get_token()
    j = c.get(f"/api/run_changes?session_id={session}&run_index=1").get_json()
    assert j["ok"] and len(j["files"]) == 1
    assert j["added"] == 1 and j["removed"] == 1


def test_api_diff(session, proj):
    p = _mk(proj, "a.py", "x = 1\n")
    tools.write_file(p, "x = 2\n")
    import app as app_module
    c = app_module.app.test_client()
    import auth
    c.environ_base["HTTP_X_AUTH_TOKEN"] = auth.get_token()
    j = c.get(f"/api/run_change_diff?session_id={session}&run_index=1"
              f"&path={p}").get_json()
    assert j["ok"] and "+x = 2" in j["diff"]


def test_api_history(session, proj):
    p = _mk(proj, "a.py", "x = 1\n")
    tools.write_file(p, "x = 2\n")
    import app as app_module
    c = app_module.app.test_client()
    import auth
    c.environ_base["HTTP_X_AUTH_TOKEN"] = auth.get_token()
    j = c.get(f"/api/run_changes/history?session_id={session}").get_json()
    assert j["ok"] and j["rounds"][0]["run_index"] == 1


def test_api_errors(client, session):
    assert client.get("/api/run_changes?session_id=999999&run_index=1").status_code == 404
    assert client.get(f"/api/run_changes?session_id={session}&run_index=x").status_code == 400
    assert client.get(f"/api/run_change_diff?session_id={session}&run_index=1").status_code == 400
    assert client.get(f"/api/run_change_diff?session_id={session}&run_index=1"
                      "&path=/nope").status_code == 404


def test_api_requires_auth(anon_client):
    assert anon_client.get("/api/run_changes?session_id=1&run_index=1").status_code == 401
    assert anon_client.get("/api/run_changes/history?session_id=1").status_code == 401


# ============ diff 健壮性 ============


def test_huge_diff_is_truncated(session, proj):
    """大文件改动必须截断 —— 否则接口和前端都会被拖垮。"""
    p = _mk(proj, "big.py", "\n".join(f"line{i}" for i in range(8000)) + "\n")
    tools.write_file(p, "\n".join(f"CHANGED{i}" for i in range(8000)) + "\n")
    d = db.get_run_change_diff(session, 1, p)
    assert d["truncated"] is True
    assert len(d["diff"]) <= tools.MAX_DIFF_CHARS + 100


def test_binary_or_unreadable_file_does_not_crash(session, proj):
    p = os.path.join(proj, "bin.dat")
    with open(p, "wb") as f:
        f.write(b"\xff\xfe\x00\x01binary")
    tools.write_file(p, "now text\n")     # 不该因为读不出原文就失败
    assert db.get_run_changes(session, 1), "二进制文件改写也应记录（尽力而为）"


def test_new_file_diff_has_no_deletions(session, proj):
    tools.create_file(os.path.join(proj, "n.py"), "a\nb\n")
    d = db.get_run_change_diff(session, 1, os.path.join(proj, "n.py"))
    assert d["removed"] == 0 and d["added"] == 2
