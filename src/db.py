"""SQLAlchemy 仓储层（MySQL）。

**接口与旧版 SQLite 实现完全一致** —— 上层（app.py / tools.py / runtime.py）与
804 个单测都只认这些函数名和返回结构，所以这次换存储引擎不需要动调用方。

实现上从「每次开一个 sqlite3 连接手写 SQL」改成「SQLAlchemy 2.0 ORM + 会话」：
- 连接池由引擎统一管理（见 ``persistence.engine``）
- 事务边界由 ``get_session()`` 负责：正常提交 / 异常回滚 / 必定关闭
- 时间仍是 ``YYYY-MM-DD HH:MM:SS`` 字符串，前端解析逻辑不用改
"""

from __future__ import annotations

import json
import os
from datetime import datetime

from sqlalchemy import delete, func, select, update

from config import DEFAULT_PERMISSION_LEVEL
from persistence import get_session as _tx
from persistence import init_schema
from persistence.models import (
    Document,
    Memory,
    Message,
    RunChange,
    RuntimeState,
    Workspace,
)
from persistence.models import (
    Session as SessionModel,
)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_db():
    """建表（幂等）+ 保证至少有一个默认工作区。"""
    init_schema()
    _ensure_default_workspace()


def default_workspace_path() -> str:
    """新建工作区时的默认位置。

    优先用 ``~/PycharmProjects``；该目录不存在时退回用户主目录，
    避免给出一个用不了的路径。
    """
    configured = os.environ.get("AGENT_DEFAULT_WORKSPACE")
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    candidate = os.path.join(os.path.expanduser("~"), "PycharmProjects")
    return candidate if os.path.isdir(candidate) else os.path.expanduser("~")


def _ensure_default_workspace():
    """保证至少有一个工作区，并把没有归属的老会话迁进去。"""
    with _tx() as db:
        first = db.scalars(select(Workspace).order_by(Workspace.id).limit(1)).first()
        if first is None:
            now = _now()
            path = default_workspace_path()
            ws = Workspace(
                name=os.path.basename(path.rstrip(os.sep)) or "默认工作区",
                path=path,
                created_at=now,
                last_used_at=now,
            )
            db.add(ws)
            db.flush()
            default_id = ws.id
        else:
            default_id = first.id

        db.execute(
            update(SessionModel)
            .where(SessionModel.workspace_id.is_(None))
            .values(workspace_id=default_id)
        )


# ============ 会话 ============


def create_session(title: str = "新对话", workspace_id: int | None = None) -> int:
    """创建新会话，返回 session_id。"""
    now = _now()
    with _tx() as db:
        if workspace_id is None:
            workspace_id = db.scalars(select(Workspace.id).order_by(Workspace.id).limit(1)).first()
        s = SessionModel(
            title=title,
            created_at=now,
            updated_at=now,
            workspace_id=workspace_id,
            permission_level=DEFAULT_PERMISSION_LEVEL,
        )
        db.add(s)
        db.flush()
        return int(s.id)


def get_all_sessions(workspace_id: int | None = None):
    """会话列表，按更新时间倒序，附带消息条数（侧边栏显示用）。"""
    count_sq = (
        select(func.count(Message.id))
        .where(Message.session_id == SessionModel.id)
        .correlate(SessionModel)
        .scalar_subquery()
    )
    stmt = select(SessionModel, count_sq.label("message_count"))
    if workspace_id is not None:
        stmt = stmt.where(SessionModel.workspace_id == workspace_id)
    stmt = stmt.order_by(SessionModel.updated_at.desc())

    with _tx() as db:
        rows = db.execute(stmt).all()
    return [
        {
            "id": s.id,
            "title": s.title,
            "created_at": s.created_at,
            "updated_at": s.updated_at,
            "workspace_id": s.workspace_id,
            "permission_level": s.permission_level,
            "message_count": n or 0,
        }
        for s, n in rows
    ]


def get_session_row(session_id: int):
    """取单个会话（含所属工作区 id）。"""
    with _tx() as db:
        s = db.get(SessionModel, session_id)
        if s is None:
            return None
        return {
            "id": s.id,
            "title": s.title,
            "created_at": s.created_at,
            "updated_at": s.updated_at,
            "workspace_id": s.workspace_id,
            "permission_level": s.permission_level,
        }


def set_permission_level(session_id: int, level: int) -> bool:
    """设置会话权限等级（1 只读 / 2 确认 / 3 完全）。"""
    if level not in (1, 2, 3):
        return False
    with _tx() as db:
        db.execute(
            update(SessionModel)
            .where(SessionModel.id == session_id)
            .values(permission_level=level)
        )
    return True


def get_permission_level(session_id: int) -> int:
    """读会话权限等级；查不到时给安全的默认值。"""
    with _tx() as db:
        level = db.scalar(
            select(SessionModel.permission_level).where(SessionModel.id == session_id)
        )
    if level not in (1, 2, 3):
        return DEFAULT_PERMISSION_LEVEL
    return int(level)


def update_session_title(session_id: int, title: str):
    with _tx() as db:
        db.execute(
            update(SessionModel).where(SessionModel.id == session_id).values(title=title)
        )


def delete_session(session_id: int):
    """删除会话及它的所有消息。"""
    with _tx() as db:
        db.execute(delete(Message).where(Message.session_id == session_id))
        db.execute(delete(SessionModel).where(SessionModel.id == session_id))


def count_user_messages(session_id: int) -> int:
    """该会话有几条用户消息 —— **这就是「第几轮」的来源**。"""
    with _tx() as db:
        n = db.scalar(
            select(func.count(Message.id)).where(
                Message.session_id == session_id, Message.role == "user"
            )
        )
    return int(n or 0)


# ============ 工作区 ============


def create_workspace(name: str, path: str) -> int:
    now = _now()
    with _tx() as db:
        ws = Workspace(
            name=name,
            path=os.path.abspath(os.path.expanduser(path)),
            created_at=now,
            last_used_at=now,
        )
        db.add(ws)
        db.flush()
        return int(ws.id)


def get_all_workspaces():
    """工作区列表，附带会话数量（侧边栏显示）。"""
    count_sq = (
        select(func.count(SessionModel.id))
        .where(SessionModel.workspace_id == Workspace.id)
        .correlate(Workspace)
        .scalar_subquery()
    )
    stmt = select(Workspace, count_sq.label("n")).order_by(
        func.coalesce(Workspace.last_used_at, Workspace.created_at).desc(), Workspace.id.desc()
    )
    with _tx() as db:
        rows = db.execute(stmt).all()
    return [
        {
            "id": w.id,
            "name": w.name,
            "path": w.path,
            "created_at": w.created_at,
            "last_used_at": w.last_used_at,
            "session_count": n or 0,
            "exists": os.path.isdir(w.path),
        }
        for w, n in rows
    ]


def get_workspace(workspace_id: int):
    with _tx() as db:
        w = db.get(Workspace, workspace_id)
        if w is None:
            return None
        return {
            "id": w.id,
            "name": w.name,
            "path": w.path,
            "created_at": w.created_at,
            "last_used_at": w.last_used_at,
            "exists": os.path.isdir(w.path),
        }


def get_workspace_open_files(workspace_id: int) -> list:
    """该工作区「右侧文件面板」打开过的文件路径。"""
    with _tx() as db:
        raw = db.scalar(select(Workspace.open_files).where(Workspace.id == workspace_id))
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return [p for p in data if isinstance(p, str)] if isinstance(data, list) else []
    except Exception:                                        # noqa: BLE001
        return []


def set_workspace_open_files(workspace_id: int, paths: list) -> None:
    clean = [p for p in (paths or []) if isinstance(p, str) and p.strip()][:50]
    with _tx() as db:
        db.execute(
            update(Workspace)
            .where(Workspace.id == workspace_id)
            .values(open_files=json.dumps(clean, ensure_ascii=False))
        )


def get_workspace_of_session(session_id: int):
    """取某个会话所属的工作区（用于决定工作目录）。"""
    with _tx() as db:
        w = db.scalars(
            select(Workspace)
            .join(SessionModel, SessionModel.workspace_id == Workspace.id)
            .where(SessionModel.id == session_id)
        ).first()
        if w is None:
            return None
        return {
            "id": w.id,
            "name": w.name,
            "path": w.path,
            "created_at": w.created_at,
            "last_used_at": w.last_used_at,
        }


def update_workspace(workspace_id: int, name: str | None = None, path: str | None = None):
    values = {}
    if name is not None:
        values["name"] = name
    if path is not None:
        values["path"] = os.path.abspath(os.path.expanduser(path))
    if not values:
        return False
    with _tx() as db:
        db.execute(update(Workspace).where(Workspace.id == workspace_id).values(**values))
    return True


def touch_workspace(workspace_id: int):
    with _tx() as db:
        db.execute(
            update(Workspace)
            .where(Workspace.id == workspace_id)
            .values(last_used_at=_now())
        )


def delete_workspace(workspace_id: int, delete_sessions: bool = True):
    """删除工作区；最后一个不允许删，避免出现「无工作区可用」。"""
    with _tx() as db:
        total = db.scalar(select(func.count(Workspace.id))) or 0
        if total <= 1:
            return False, "至少要保留一个工作区"

        sids = list(
            db.scalars(select(SessionModel.id).where(SessionModel.workspace_id == workspace_id))
        )
        if delete_sessions and sids:
            db.execute(delete(Message).where(Message.session_id.in_(sids)))
            db.execute(delete(SessionModel).where(SessionModel.id.in_(sids)))
        elif sids:
            db.execute(
                update(SessionModel)
                .where(SessionModel.workspace_id == workspace_id)
                .values(workspace_id=None)
            )
        db.execute(delete(Workspace).where(Workspace.id == workspace_id))
        return True, sids


# ============ 消息 ============


def save_message(session_id: int, role: str, content: str):
    """保存一条消息，并顺手推进会话的 updated_at（列表按它排序）。"""
    now = _now()
    with _tx() as db:
        db.add(
            Message(session_id=session_id, role=role, content=content, created_at=now)
        )
        db.execute(
            update(SessionModel)
            .where(SessionModel.id == session_id)
            .values(updated_at=now)
        )


def get_messages(session_id: int):
    with _tx() as db:
        rows = list(
            db.scalars(
                select(Message).where(Message.session_id == session_id).order_by(Message.id)
            )
        )
    return [{"role": m.role, "content": m.content, "created_at": m.created_at} for m in rows]


# ============ 本轮改动（第 N 轮）============


def record_change(
    session_id: int,
    run_index: int,
    path: str,
    change_type: str,
    tool: str = "",
    diff: str = "",
    added: int = 0,
    removed: int = 0,
    truncated: bool = False,
) -> None:
    """记一条本轮的文件改动。

    **同一个文件在同一轮里被改多次时合并**（diff 追加、行数累加），
    并保留最有信息量的类型：删除 > 新建 > 修改。
    """
    with _tx() as db:
        row = db.scalars(
            select(RunChange).where(
                RunChange.session_id == session_id,
                RunChange.run_index == run_index,
                RunChange.path == path,
            )
        ).first()

        if row is None:
            db.add(
                RunChange(
                    session_id=session_id,
                    run_index=run_index,
                    path=path,
                    change_type=change_type,
                    tool=tool,
                    diff=diff,
                    added=added,
                    removed=removed,
                    truncated=1 if truncated else 0,
                    created_at=_now(),
                )
            )
            return

        if change_type == "delete":
            merged_type = "delete"
        elif row.change_type == "create":
            merged_type = "create"
        else:
            merged_type = change_type

        row.change_type = merged_type
        row.tool = tool
        row.diff = ((row.diff or "") + "\n" + diff) if diff else (row.diff or "")
        row.added = (row.added or 0) + added
        row.removed = (row.removed or 0) + removed
        row.truncated = 1 if truncated else 0


def get_run_changes(session_id: int, run_index: int) -> list:
    """某一轮改了哪些文件（不含 diff 正文）。"""
    with _tx() as db:
        rows = list(
            db.scalars(
                select(RunChange)
                .where(RunChange.session_id == session_id, RunChange.run_index == run_index)
                .order_by(RunChange.id)
            )
        )
    return [
        {
            "path": r.path,
            "change_type": r.change_type,
            "tool": r.tool,
            "added": r.added or 0,
            "removed": r.removed or 0,
            "truncated": bool(r.truncated),
            "created_at": r.created_at,
        }
        for r in rows
    ]


def get_run_change_diff(session_id: int, run_index: int, path: str):
    with _tx() as db:
        r = db.scalars(
            select(RunChange).where(
                RunChange.session_id == session_id,
                RunChange.run_index == run_index,
                RunChange.path == path,
            )
        ).first()
        if r is None:
            return None
        return {
            "diff": r.diff or "",
            "added": r.added or 0,
            "removed": r.removed or 0,
            "truncated": bool(r.truncated),
            "change_type": r.change_type,
        }


def get_changed_run_indexes(session_id: int) -> list:
    """该会话哪些轮次有改动记录（倒序）。"""
    stmt = (
        select(
            RunChange.run_index,
            func.count(RunChange.id),
            func.sum(RunChange.added),
            func.sum(RunChange.removed),
        )
        .where(RunChange.session_id == session_id)
        .group_by(RunChange.run_index)
        .order_by(RunChange.run_index.desc())
    )
    with _tx() as db:
        rows = db.execute(stmt).all()
    return [
        {"run_index": r[0], "files": r[1], "added": r[2] or 0, "removed": r[3] or 0}
        for r in rows
    ]


# ============ 文档（RAG 元数据）============


def add_document(filename: str, chunk_count: int) -> int:
    with _tx() as db:
        doc = Document(filename=filename, chunk_count=chunk_count, created_at=_now())
        db.add(doc)
        db.flush()
        return int(doc.id)


def update_document_chunk_count(doc_id: int, chunk_count: int):
    with _tx() as db:
        db.execute(
            update(Document).where(Document.id == doc_id).values(chunk_count=chunk_count)
        )


def get_all_documents():
    with _tx() as db:
        rows = list(db.scalars(select(Document).order_by(Document.id.desc())))
    return [
        {
            "id": d.id,
            "filename": d.filename,
            "chunk_count": d.chunk_count,
            "created_at": d.created_at,
        }
        for d in rows
    ]


def delete_document(doc_id: int):
    with _tx() as db:
        db.execute(delete(Document).where(Document.id == doc_id))


# ============ 长期记忆 ============


def save_memory(content: str) -> int:
    with _tx() as db:
        m = Memory(content=content, created_at=_now())
        db.add(m)
        db.flush()
        return int(m.id)


def get_all_memories():
    with _tx() as db:
        rows = list(db.scalars(select(Memory).order_by(Memory.id.desc())))
    return [{"id": m.id, "content": m.content, "created_at": m.created_at} for m in rows]


def delete_memory(memory_id: int):
    with _tx() as db:
        db.execute(delete(Memory).where(Memory.id == memory_id))


# ============ 运行时状态（会话级粘性状态）============


def upsert_runtime_state(session_key: str, kind: str, payload: str, updated_at: float):
    """写入 / 覆盖某会话某类状态的 JSON 载荷。

    用「先查后写」而不是 MySQL 的 ``ON DUPLICATE KEY UPDATE``：
    同一会话同一类状态只有一行，并发写极少，可移植性更重要（单测走 SQLite）。
    """
    with _tx() as db:
        row = db.get(RuntimeState, (session_key, kind))
        if row is None:
            db.add(
                RuntimeState(
                    session_key=session_key, kind=kind, payload=payload, updated_at=updated_at
                )
            )
        else:
            row.payload = payload
            row.updated_at = updated_at


def get_runtime_state(session_key: str, kind: str):
    """读取某会话某类状态，返回 (payload, updated_at)；不存在返回 None。"""
    with _tx() as db:
        row = db.get(RuntimeState, (session_key, kind))
        if row is None:
            return None
        return (row.payload, row.updated_at)


def delete_runtime_state(session_key: str, kind: str = None):
    with _tx() as db:
        stmt = delete(RuntimeState).where(RuntimeState.session_key == session_key)
        if kind is not None:
            stmt = stmt.where(RuntimeState.kind == kind)
        db.execute(stmt)


def delete_runtime_state_kind(kind: str):
    with _tx() as db:
        db.execute(delete(RuntimeState).where(RuntimeState.kind == kind))


# ---- 兼容旧调用名 ----
# 原实现把「取单个会话」叫 get_session；而 persistence 里的事务上下文同名，
# 已在顶部以 _tx 别名导入，这里把 get_session 这个名字留给数据查询，调用方无需改。
def get_session(session_id: int):
    """取单个会话（旧接口名，等价于 get_session_row）。"""
    return get_session_row(session_id)
