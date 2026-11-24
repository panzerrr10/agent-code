"""工作区（Workspace）回归测试。

工作区是「工作目录的唯一真相源」：以前项目路径既存在前端 localStorage（全局一份）
又存在后端按会话状态里（每会话一份），切换会话时前者会把后者的设置静默覆盖。
这里把新的层级关系（工作区 → 会话 → 工作目录）固化下来。
"""

import os
import shutil
import tempfile

import pytest

import db
import runtime

HOME = os.path.expanduser("~")


@pytest.fixture
def work(monkeypatch):
    """home 下建临时工作区根，并让默认工作区落在里面，避免污染真实目录。"""
    d = tempfile.mkdtemp(prefix=".ws-test-", dir=HOME)
    monkeypatch.setenv("AGENT_DEFAULT_WORKSPACE", d)
    yield d
    runtime.reset_all()
    runtime.set_session_id(None)
    shutil.rmtree(d, ignore_errors=True)


def _wipe_workspaces():
    """清空三张表（走 ORM，SQLite / MySQL 都适用）。"""
    from sqlalchemy import delete

    from persistence import get_session
    from persistence.models import Message, Workspace
    from persistence.models import Session as SessionModel

    with get_session() as s:
        s.execute(delete(Message))
        s.execute(delete(SessionModel))
        s.execute(delete(Workspace))


# ============ 默认工作区 ============


def test_default_workspace_path_is_pycharmprojects(monkeypatch):
    """不选位置时默认用 ~/PycharmProjects。"""
    monkeypatch.delenv("AGENT_DEFAULT_WORKSPACE", raising=False)
    expected = os.path.join(HOME, "PycharmProjects")
    if os.path.isdir(expected):
        assert db.default_workspace_path() == expected
    else:
        assert db.default_workspace_path() == HOME


def test_default_workspace_path_respects_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DEFAULT_WORKSPACE", str(tmp_path))
    assert db.default_workspace_path() == str(tmp_path)


def test_init_creates_default_workspace(work):
    _wipe_workspaces()
    db.init_db()
    ws = db.get_all_workspaces()
    assert len(ws) == 1
    assert ws[0]["path"] == work
    assert ws[0]["exists"] is True


# ============ 会话归属 ============


def test_new_session_belongs_to_workspace(work):
    _wipe_workspaces()
    db.init_db()
    a = db.create_workspace("A", os.path.join(work, "a"))
    sid = db.create_session("任务", a)

    assert db.get_session(sid)["workspace_id"] == a
    assert [s["id"] for s in db.get_all_sessions(a)] == [sid]
    assert db.get_all_sessions(-1) == []


def test_session_without_workspace_falls_back_to_first(work):
    _wipe_workspaces()
    db.init_db()
    sid = db.create_session("来源不明")
    first = db.get_all_workspaces()[0]["id"]
    assert db.get_session(sid)["workspace_id"] == first, "没指定工作区时应落到默认工作区"


def test_get_workspace_of_session(work):
    _wipe_workspaces()
    db.init_db()
    a = db.create_workspace("A", os.path.join(work, "a"))
    sid = db.create_session("x", a)
    assert db.get_workspace_of_session(sid)["id"] == a
    assert db.get_workspace_of_session(999999) is None


# ============ 工作目录解析（核心：消除双真相源）============


def test_project_root_follows_workspace(work):
    _wipe_workspaces()
    db.init_db()
    a = db.create_workspace("A", os.path.join(work, "proj-a"))
    sid = db.create_session("x", a)

    assert runtime.get_project_root(sid) == os.path.join(work, "proj-a"), (
        "会话的工作目录应来自所属工作区"
    )


def test_explicit_override_wins_but_can_be_cleared(work):
    _wipe_workspaces()
    db.init_db()
    a = db.create_workspace("A", os.path.join(work, "proj-a"))
    sid = db.create_session("x", a)

    runtime.set_project_root("/tmp/覆盖", sid)
    assert runtime.get_project_root(sid) == "/tmp/覆盖"

    runtime.clear_project_root(sid)
    assert runtime.get_project_root(sid) == os.path.join(work, "proj-a"), (
        "清掉覆盖后应回落到工作区路径（否则老会话的历史覆盖会一直盖住工作区）"
    )


def test_two_sessions_in_different_workspaces_do_not_interfere(work):
    """这正是旧实现的 bug：A 设了路径后 B 再设，切回 A 会拿到 B 的值。"""
    _wipe_workspaces()
    db.init_db()
    a = db.create_workspace("A", os.path.join(work, "a"))
    b = db.create_workspace("B", os.path.join(work, "b"))
    sa = db.create_session("在A", a)
    sb = db.create_session("在B", b)

    runtime.clear_project_root(sa)
    runtime.clear_project_root(sb)
    assert runtime.get_project_root(sa) == os.path.join(work, "a")
    assert runtime.get_project_root(sb) == os.path.join(work, "b")


# ============ 增删改 ============


def test_create_and_update_workspace(work):
    _wipe_workspaces()
    db.init_db()
    wid = db.create_workspace("旧名", os.path.join(work, "p1"))
    assert db.get_workspace(wid)["name"] == "旧名"

    db.update_workspace(wid, name="新名", path=os.path.join(work, "p2"))
    got = db.get_workspace(wid)
    assert got["name"] == "新名"
    assert got["path"] == os.path.join(work, "p2")


def test_delete_workspace_cascades_sessions_and_messages(work):
    _wipe_workspaces()
    db.init_db()
    keep = db.get_all_workspaces()[0]["id"]
    wid = db.create_workspace("要删的", os.path.join(work, "gone"))
    sid = db.create_session("x", wid)
    db.save_message(sid, "user", "hi")

    ok, sids = db.delete_workspace(wid)
    assert ok and sids == [sid]
    assert db.get_workspace(wid) is None
    assert db.get_messages(sid) == [], "消息应随之删除，不能留孤儿"
    assert db.get_all_workspaces()[0]["id"] == keep, "别的工区不受影响"


def test_cannot_delete_last_workspace(work):
    _wipe_workspaces()
    db.init_db()
    only = db.get_all_workspaces()[0]["id"]
    ok, msg = db.delete_workspace(only)
    assert not ok and "至少" in msg
    assert len(db.get_all_workspaces()) == 1


def test_workspace_session_count(work):
    _wipe_workspaces()
    db.init_db()
    wid = db.create_workspace("计数", os.path.join(work, "c"))
    for _ in range(3):
        db.create_session("x", wid)
    row = next(w for w in db.get_all_workspaces() if w["id"] == wid)
    assert row["session_count"] == 3


# ============ 迁移：老库升级 ============


def test_init_db_is_idempotent(work):
    _wipe_workspaces()
    for _ in range(3):
        db.init_db()
    assert len(db.get_all_workspaces()) == 1, "重复初始化不应重复建工作区"


# ============ HTTP 接口 ============


def test_api_workspace_flow(client, work, monkeypatch):
    monkeypatch.setenv("AGENT_DEFAULT_WORKSPACE", work)

    r = client.get("/workspaces").get_json()
    assert r["default_path"] == work

    # 不填位置 -> 用默认位置
    r = client.post("/workspaces", json={"name": "", "path": ""}).get_json()
    assert r["ok"] and r["path"] == work

    # 名称留空 -> 取目录名
    target = os.path.join(work, "my-proj")
    r = client.post("/workspaces", json={"path": target}).get_json()
    assert r["name"] == "my-proj"
    assert os.path.isdir(target), "目录不存在时应自动建出来"

    wid = r["id"]
    s = client.post("/sessions", json={"workspace_id": wid}).get_json()
    assert s["workspace_id"] == wid
    assert [x["id"] for x in client.get(f"/sessions?workspace_id={wid}").get_json()] == [s["id"]]

    # 改名
    assert client.patch(f"/workspaces/{wid}", json={"name": "改过"}).get_json()["workspace"][
        "name"
    ] == "改过"

    # 删除
    assert client.delete(f"/workspaces/{wid}").get_json()["deleted_sessions"] == 1
    assert client.delete(f"/workspaces/{wid}").status_code == 404


def test_api_rejects_sensitive_path(client):
    r = client.post("/workspaces", json={"name": "x", "path": "/etc"})
    assert r.status_code == 400
    assert "不允许" in r.get_json()["error"]


def test_api_session_with_unknown_workspace(client):
    r = client.post("/sessions", json={"workspace_id": 999999})
    assert r.status_code == 404


def test_api_sessions_without_body_still_works(client):
    """回归：request.json 在没有 body 时会 415，接口会整个挂掉。"""
    r = client.post("/sessions")
    assert r.status_code == 200
    assert r.get_json()["id"]
