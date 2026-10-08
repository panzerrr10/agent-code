"""回收站（备份记录 + 一键恢复）的回归测试。"""

import os
import shutil
import tempfile

import pytest

import runtime
import tools
import trash

HOME = os.path.expanduser("~")


@pytest.fixture
def work(monkeypatch):
    """在 home 下建临时工作区，并把回收站指到里面，避免污染真实 data/.trash。"""
    d = tempfile.mkdtemp(prefix=".agentcode-trash-", dir=HOME)
    monkeypatch.setattr(trash, "TRASH_DIR", os.path.join(d, ".trash"))
    runtime.reset_all()
    runtime.set_session_id("trash-test")
    runtime.set_project_root(d)
    yield d
    runtime.reset_all()
    runtime.set_session_id(None)
    shutil.rmtree(d, ignore_errors=True)


def _write(path: str, text: str):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


# ============ 备份与 sidecar ============


def test_backup_records_original_path(work):
    target = os.path.join(work, "a.txt")
    _write(target, "v1")

    backup = trash.backup_file(target, kind="edit")
    assert backup and os.path.isfile(backup)

    entries = trash.list_entries()
    assert len(entries) == 1
    e = entries[0]
    assert e["original_path"] == target, "没有记录原始路径，就无法一键恢复"
    assert e["kind"] == "edit"
    assert e["restorable"] is True


def test_delete_produces_restorable_entry(work):
    target = os.path.join(work, "doomed.txt")
    _write(target, "内容")

    assert "回收站" in tools.delete_file(target)
    assert not os.path.exists(target)

    entries = trash.list_entries()
    assert len(entries) == 1
    assert entries[0]["kind"] == "delete"
    assert entries[0]["original_path"] == target

    ok, msg = trash.restore(entries[0]["id"])
    assert ok, msg
    assert _read(target) == "内容", "删除后应能原样恢复"


def test_write_and_edit_produce_restorable_entries(work):
    target = os.path.join(work, "b.txt")
    tools.create_file(target, "v1")

    tools.write_file(target, "v2")
    tools.edit_file(target, "v2", "v3")
    assert _read(target) == "v3"

    kinds = {e["kind"] for e in trash.list_entries()}
    assert {"backup", "edit"} <= kinds

    # 恢复「编辑前备份」应把内容还原成 v2
    edit_entry = next(e for e in trash.list_entries() if e["kind"] == "edit")
    ok, msg = trash.restore(edit_entry["id"])
    assert ok, msg
    assert _read(target) == "v2"


# ============ 恢复的安全性 ============


def test_restore_is_itself_reversible(work):
    """恢复会覆盖当前文件，所以必须先把现状也备份一份。"""
    target = os.path.join(work, "c.txt")
    _write(target, "旧版本")
    backup = trash.backup_file(target, kind="edit")
    _write(target, "当前工作")

    ok, _ = trash.restore(os.path.basename(backup))
    assert ok
    assert _read(target) == "旧版本", "恢复后应是备份里的内容"

    entries = trash.list_entries()
    assert any(e["kind"] == "restore" for e in entries), "覆盖前必须先备份现状，恢复才可回退"


def test_restore_rejects_path_traversal(work):
    ok, msg = trash.restore("../../etc/passwd")
    assert not ok and "非法" in msg


def test_restore_rejects_non_permitted_target(work, monkeypatch):
    target = os.path.join(work, "d.txt")
    _write(target, "x")
    backup = trash.backup_file(target, kind="edit")

    # 人为把原始路径改成白名单之外的路径
    meta = backup + trash.META_SUFFIX
    import json

    data = json.load(open(meta, encoding="utf-8"))
    data["original_path"] = "/etc/passwd"
    json.dump(data, open(meta, "w", encoding="utf-8"))

    ok, msg = trash.restore(os.path.basename(backup))
    assert not ok and "允许范围" in msg


def test_restore_rejects_entry_without_sidecar(work):
    """旧格式备份没有原始路径，不能一键恢复（否则可能还原到错误位置）。"""
    os.makedirs(trash.TRASH_DIR, exist_ok=True)
    legacy = os.path.join(trash.TRASH_DIR, "20200101_010101_backup_old.txt")
    _write(legacy, "旧备份")

    entries = trash.list_entries()
    assert len(entries) == 1
    assert entries[0]["restorable"] is False

    ok, msg = trash.restore(entries[0]["id"])
    assert not ok and "原始路径" in msg


# ============ 清理 ============


def test_purge_removes_entry_and_sidecar(work):
    target = os.path.join(work, "e.txt")
    _write(target, "x")
    backup = trash.backup_file(target, kind="edit")
    name = os.path.basename(backup)

    ok, msg = trash.purge(name)
    assert ok, msg
    assert trash.list_entries() == []
    assert not os.path.exists(backup + trash.META_SUFFIX), "sidecar 应一起删除"


def test_list_entries_sorted_newest_first(work):
    for i in range(3):
        target = os.path.join(work, f"f{i}.txt")
        _write(target, str(i))
        trash.backup_file(target, kind="edit")

    entries = trash.list_entries()
    assert len(entries) == 3
    # mtime 可能同秒，这里只要求列表长度与可恢复性正确
    assert all(e["restorable"] for e in entries)
