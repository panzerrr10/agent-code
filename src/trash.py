"""回收站：列出 / 恢复 `.trash` 里的备份。

**为什么需要 sidecar**：备份文件名形如 `20261008_101010_backup_foo.py`，
光看名字只知道「哪个文件的备份、什么时候」——**不知道原始路径**，
所以没法一键还原（不同项目下可能有一堆同名文件）。

因此每次备份/删除都会额外写一份 `<备份名>.meta.json`：

    {"original_path": "/abs/path/foo.py", "kind": "backup", "created_at": "..."}

早期没有 sidecar 的旧备份仍会列出，但标记为「原始路径未知」，不做一键恢复。
"""

from __future__ import annotations

import json
import os
import shutil
import time

from paths import TRASH_DIR, is_path_permitted

META_SUFFIX = ".meta.json"

_KIND_LABELS = {
    "delete": "删除",
    "backup": "覆盖前备份",
    "edit": "编辑前备份",
    "multiedit": "批量编辑前备份",
    "restore": "恢复前备份",
}


def _meta_path(trash_path: str) -> str:
    return trash_path + META_SUFFIX


def _write_meta(trash_path: str, original_path: str, kind: str) -> None:
    try:
        with open(_meta_path(trash_path), "w", encoding="utf-8") as f:
            json.dump(
                {
                    "original_path": os.path.abspath(original_path),
                    "kind": kind,
                    "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                },
                f,
                ensure_ascii=False,
            )
    except Exception:
        # sidecar 写失败不影响主流程，只是这条备份无法一键恢复
        pass


def _read_meta(trash_path: str) -> dict:
    try:
        with open(_meta_path(trash_path), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _infer_kind(name: str) -> str:
    """老备份没有 sidecar 时，从文件名猜一下操作类型。"""
    for key in ("multiedit", "backup", "edit"):
        if f"_{key}_" in name:
            return key
    return "delete"


def backup_file(abs_path: str, kind: str = "backup") -> str | None:
    """把文件复制一份到回收站，并写 sidecar 记录原始路径。返回备份路径。

    这是写入/编辑类操作统一的前置备份入口。
    """
    try:
        os.makedirs(TRASH_DIR, exist_ok=True)
        filename = os.path.basename(abs_path)
        # 同一秒内多次备份同一个文件时，加序号避免互相覆盖
        stamp = time.strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(TRASH_DIR, f"{stamp}_{kind}_{filename}")
        seq = 1
        while os.path.exists(backup_path):
            backup_path = os.path.join(TRASH_DIR, f"{stamp}_{kind}_{seq}_{filename}")
            seq += 1
        shutil.copy2(abs_path, backup_path)
        _write_meta(backup_path, abs_path, kind)
        return backup_path
    except Exception:
        return None


def move_to_trash(abs_path: str) -> str | None:
    """把文件移入回收站（删除语义），并写 sidecar。返回回收站路径。"""
    try:
        os.makedirs(TRASH_DIR, exist_ok=True)
        filename = os.path.basename(abs_path)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        trash_path = os.path.join(TRASH_DIR, f"{stamp}_{filename}")
        seq = 1
        while os.path.exists(trash_path):
            trash_path = os.path.join(TRASH_DIR, f"{stamp}_{seq}_{filename}")
            seq += 1
        shutil.move(abs_path, trash_path)
        _write_meta(trash_path, abs_path, "delete")
        return trash_path
    except Exception:
        return None


def list_entries() -> list[dict]:
    """列出回收站条目（按时间倒序）。"""
    if not os.path.isdir(TRASH_DIR):
        return []
    entries = []
    try:
        names = os.listdir(TRASH_DIR)
    except Exception:
        return []

    for name in names:
        if name.endswith(META_SUFFIX):
            continue  # sidecar 不是条目本身
        path = os.path.join(TRASH_DIR, name)
        if not os.path.isfile(path):
            continue
        meta = _read_meta(path)
        try:
            stat = os.stat(path)
            size, mtime = stat.st_size, stat.st_mtime
        except Exception:
            size, mtime = 0, 0
        original = meta.get("original_path")
        kind = meta.get("kind") or _infer_kind(name)
        entries.append(
            {
                "id": name,
                "name": name,
                "size": size,
                "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)),
                "original_path": original or "",
                "kind": kind,
                "kind_label": _KIND_LABELS.get(kind, kind),
                "restorable": bool(original),
            }
        )

    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return entries


def restore(entry_id: str) -> tuple[bool, str]:
    """把某条备份恢复到它的原始路径。

    两点安全考虑：
    1. `entry_id` 只允许是纯文件名，杜绝 `../` 目录穿越；
    2. 如果目标路径当前**已存在**，先把现有文件也备份进回收站——
       这样「恢复」本身也是可回滚的，不会静默覆盖用户现在的工作。
    """
    if not entry_id or os.path.basename(entry_id) != entry_id:
        return False, "非法的回收站条目名"

    src = os.path.join(TRASH_DIR, entry_id)
    if not os.path.isfile(src):
        return False, "回收站条目不存在"

    meta = _read_meta(src)
    original = meta.get("original_path")
    if not original:
        return False, "这条备份没有记录原始路径（旧格式），请手动从 .trash 目录恢复"

    if not is_path_permitted(original):
        return False, f"目标路径不在允许范围内，已拒绝：{original}"

    if os.path.exists(original):
        # 先把现状备份，保证恢复可逆
        if os.path.isfile(original):
            backup_file(original, kind="restore")

    try:
        parent = os.path.dirname(original)
        if parent:
            os.makedirs(parent, exist_ok=True)
        shutil.copy2(src, original)
    except Exception as e:
        return False, f"恢复失败：{e}"

    return True, f"已恢复到 {original}"


def purge(entry_id: str) -> tuple[bool, str]:
    """彻底删除某条回收站记录（连同 sidecar）。"""
    if not entry_id or os.path.basename(entry_id) != entry_id:
        return False, "非法的回收站条目名"
    src = os.path.join(TRASH_DIR, entry_id)
    if not os.path.isfile(src):
        return False, "回收站条目不存在"
    try:
        os.remove(src)
        meta = _meta_path(src)
        if os.path.isfile(meta):
            os.remove(meta)
    except Exception as e:
        return False, f"删除失败：{e}"
    return True, f"已彻底删除 {entry_id}"
