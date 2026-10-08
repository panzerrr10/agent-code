"""SQLite 存储层（工作区 / 会话 / 消息 / 文档 / 长期记忆）。

设计说明：
- 统一通过 ``_connect()`` 建连：开启 WAL 提升读写并发，设置 busy_timeout
  避免多个请求同时写库时直接抛 "database is locked"。
- 对最热的查询路径建索引（messages.session_id、sessions.updated_at）。
- **工作区是工作目录的唯一真相源**：会话归属工作区，会话的工作目录取自
  所属工作区的 path，不再依赖前端 localStorage。
"""

import os
import sqlite3
from datetime import datetime

from paths import DB_PATH


def _connect():
    """建连并设置通用 PRAGMA。"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db():
    """初始化数据库，创建所有表与索引（幂等）"""
    conn = _connect()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            chunk_count INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    # 运行时状态（断点续跑的挂起状态 / 计划 / 项目根目录）
    # 之前这些只存在内存里，服务一重启就丢：用户点「确认」会找不到挂起状态。
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS runtime_state (
            session_key TEXT NOT NULL,
            kind        TEXT NOT NULL,
            payload     TEXT NOT NULL,
            updated_at  REAL NOT NULL,
            PRIMARY KEY (session_key, kind)
        )
    """)

    # 工作区：一组会话 + 一个工作目录。
    # 引入它是为了解决「项目路径既存在前端 localStorage 又存在后端按会话状态里」
    # 造成的双真相源问题：切换会话时前端拿的是全局值，会把别的会话的设置覆盖掉。
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS workspaces (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT NOT NULL,
            path         TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            last_used_at TEXT
        )
    """)

    # 会话归属工作区。老库没有这一列，下面做一次迁移（SQLite 的 ADD COLUMN 幂等性
    # 要自己判断，所以先查 PRAGMA）。
    cols = {row[1] for row in cursor.execute("PRAGMA table_info(sessions)")}
    if "workspace_id" not in cols:
        cursor.execute("ALTER TABLE sessions ADD COLUMN workspace_id INTEGER")

    # 索引：读会话历史是最热路径（WHERE session_id ORDER BY id）
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_sessions_workspace ON sessions(workspace_id, updated_at DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_documents_created ON documents(created_at DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_runtime_state_kind ON runtime_state(kind)"
    )

    conn.commit()
    conn.close()

    _ensure_default_workspace()


def default_workspace_path() -> str:
    """新建工作区时的默认位置。

    优先用 ``~/PycharmProjects``（本机就是 /home/pz/PycharmProjects）；
    该目录不存在时退回用户主目录，避免给出一个用不了的路径。
    """
    configured = os.environ.get("AGENT_DEFAULT_WORKSPACE")
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    candidate = os.path.join(os.path.expanduser("~"), "PycharmProjects")
    return candidate if os.path.isdir(candidate) else os.path.expanduser("~")


def _ensure_default_workspace():
    """保证至少有一个工作区，并把没有归属的老会话迁进去。"""
    conn = _connect()
    cursor = conn.cursor()
    try:
        row = cursor.execute("SELECT id FROM workspaces ORDER BY id LIMIT 1").fetchone()
        if row is None:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            path = default_workspace_path()
            cursor.execute(
                "INSERT INTO workspaces (name, path, created_at, last_used_at) VALUES (?, ?, ?, ?)",
                (os.path.basename(path.rstrip(os.sep)) or "默认工作区", path, now, now),
            )
            row = (cursor.lastrowid,)
        default_id = row[0]
        # 老会话（workspace_id 为空）统一迁到第一个工作区
        cursor.execute(
            "UPDATE sessions SET workspace_id = ? WHERE workspace_id IS NULL", (default_id,)
        )
        conn.commit()
    finally:
        conn.close()


def create_session(title: str = "新对话", workspace_id: int | None = None) -> int:
    """创建新会话，返回 session_id。

    workspace_id 为空时挂到第一个工作区（正常流程里前端一定会带上）。
    """
    conn = _connect()
    cursor = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if workspace_id is None:
        row = cursor.execute("SELECT id FROM workspaces ORDER BY id LIMIT 1").fetchone()
        workspace_id = row[0] if row else None
    cursor.execute(
        "INSERT INTO sessions (title, created_at, updated_at, workspace_id) VALUES (?, ?, ?, ?)",
        (title, now, now, workspace_id),
    )
    session_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return session_id


def get_all_sessions(workspace_id: int | None = None):
    """获取会话，按更新时间倒序；给 workspace_id 时只返回该工作区的。"""
    conn = _connect()
    cursor = conn.cursor()
    if workspace_id is None:
        cursor.execute(
            "SELECT id, title, created_at, updated_at, workspace_id "
            "FROM sessions ORDER BY updated_at DESC"
        )
    else:
        cursor.execute(
            "SELECT id, title, created_at, updated_at, workspace_id "
            "FROM sessions WHERE workspace_id = ? ORDER BY updated_at DESC",
            (workspace_id,),
        )
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "id": r[0],
            "title": r[1],
            "created_at": r[2],
            "updated_at": r[3],
            "workspace_id": r[4],
        }
        for r in rows
    ]


def get_session(session_id: int):
    """取单个会话（含所属工作区）。"""
    conn = _connect()
    row = conn.execute(
        "SELECT id, title, created_at, updated_at, workspace_id FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0],
        "title": row[1],
        "created_at": row[2],
        "updated_at": row[3],
        "workspace_id": row[4],
    }


# ============ 工作区 ============


def create_workspace(name: str, path: str) -> int:
    """新建工作区，返回 id。"""
    conn = _connect()
    cursor = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "INSERT INTO workspaces (name, path, created_at, last_used_at) VALUES (?, ?, ?, ?)",
        (name, os.path.abspath(os.path.expanduser(path)), now, now),
    )
    wid = cursor.lastrowid
    conn.commit()
    conn.close()
    return wid


def get_all_workspaces():
    """列出工作区，附带各自的会话数量（侧边栏要显示）。"""
    conn = _connect()
    rows = conn.execute(
        """
        SELECT w.id, w.name, w.path, w.created_at, w.last_used_at,
               (SELECT COUNT(*) FROM sessions s WHERE s.workspace_id = w.id)
        FROM workspaces w
        ORDER BY COALESCE(w.last_used_at, w.created_at) DESC, w.id DESC
        """
    ).fetchall()
    conn.close()
    return [
        {
            "id": r[0],
            "name": r[1],
            "path": r[2],
            "created_at": r[3],
            "last_used_at": r[4],
            "session_count": r[5],
            "exists": os.path.isdir(r[2]),
        }
        for r in rows
    ]


def get_workspace(workspace_id: int):
    conn = _connect()
    row = conn.execute(
        "SELECT id, name, path, created_at, last_used_at FROM workspaces WHERE id = ?",
        (workspace_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0],
        "name": row[1],
        "path": row[2],
        "created_at": row[3],
        "last_used_at": row[4],
        "exists": os.path.isdir(row[2]),
    }


def get_workspace_of_session(session_id: int):
    """取某个会话所属的工作区（用于决定工作目录）。"""
    conn = _connect()
    row = conn.execute(
        """
        SELECT w.id, w.name, w.path, w.created_at, w.last_used_at
        FROM sessions s JOIN workspaces w ON w.id = s.workspace_id
        WHERE s.id = ?
        """,
        (session_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0],
        "name": row[1],
        "path": row[2],
        "created_at": row[3],
        "last_used_at": row[4],
    }


def update_workspace(workspace_id: int, name: str | None = None, path: str | None = None):
    """改名 / 改路径。"""
    fields, params = [], []
    if name is not None:
        fields.append("name = ?")
        params.append(name)
    if path is not None:
        fields.append("path = ?")
        params.append(os.path.abspath(os.path.expanduser(path)))
    if not fields:
        return False
    params.append(workspace_id)
    conn = _connect()
    conn.execute(f"UPDATE workspaces SET {', '.join(fields)} WHERE id = ?", params)
    conn.commit()
    conn.close()
    return True


def touch_workspace(workspace_id: int):
    """记录最近使用时间（影响列表排序）。"""
    conn = _connect()
    conn.execute(
        "UPDATE workspaces SET last_used_at = ? WHERE id = ?",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), workspace_id),
    )
    conn.commit()
    conn.close()


def delete_workspace(workspace_id: int, delete_sessions: bool = True):
    """删除工作区。

    delete_sessions=True 时连同其下会话与消息一起删（前端会先告知数量）。
    最后一个工作区不允许删除，避免出现「无工作区可用」的空状态。
    """
    conn = _connect()
    cursor = conn.cursor()
    total = cursor.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0]
    if total <= 1:
        conn.close()
        return False, "至少要保留一个工作区"
    sids = [
        r[0]
        for r in cursor.execute(
            "SELECT id FROM sessions WHERE workspace_id = ?", (workspace_id,)
        )
    ]
    if delete_sessions and sids:
        qmarks = ",".join("?" * len(sids))
        cursor.execute(f"DELETE FROM messages WHERE session_id IN ({qmarks})", sids)
        cursor.execute(f"DELETE FROM sessions WHERE id IN ({qmarks})", sids)
    elif sids:
        cursor.execute(
            "UPDATE sessions SET workspace_id = NULL WHERE workspace_id = ?", (workspace_id,)
        )
    cursor.execute("DELETE FROM workspaces WHERE id = ?", (workspace_id,))
    conn.commit()
    conn.close()
    return True, sids


def delete_session(session_id: int):
    """删除会话及它的所有消息"""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
    cursor.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    conn.commit()
    conn.close()


def save_message(session_id: int, role: str, content: str):
    """保存一条消息"""
    conn = _connect()
    cursor = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "INSERT INTO messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
        (session_id, role, content, now),
    )
    cursor.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))
    conn.commit()
    conn.close()


def get_messages(session_id: int):
    """获取某个会话的所有消息"""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT role, content, created_at FROM messages WHERE session_id = ? ORDER BY id ASC",
        (session_id,),
    )
    rows = cursor.fetchall()
    conn.close()
    return [{"role": r[0], "content": r[1], "created_at": r[2]} for r in rows]


def update_session_title(session_id: int, title: str):
    """更新会话标题"""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))
    conn.commit()
    conn.close()


# ============ 文档相关（RAG）============
def add_document(filename: str, chunk_count: int) -> int:
    """添加文档记录，返回 doc_id"""
    conn = _connect()
    cursor = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "INSERT INTO documents (filename, chunk_count, created_at) VALUES (?, ?, ?)",
        (filename, chunk_count, now),
    )
    doc_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return doc_id


def update_document_chunk_count(doc_id: int, chunk_count: int):
    """更新文档的片段数"""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("UPDATE documents SET chunk_count = ? WHERE id = ?", (chunk_count, doc_id))
    conn.commit()
    conn.close()


def get_all_documents():
    """获取所有文档"""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT id, filename, chunk_count, created_at FROM documents ORDER BY id DESC")
    rows = cursor.fetchall()
    conn.close()
    return [{"id": r[0], "filename": r[1], "chunk_count": r[2], "created_at": r[3]} for r in rows]


def delete_document(doc_id: int):
    """删除文档记录"""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    conn.commit()
    conn.close()


# ============ 长期记忆 ============
def save_memory(content: str) -> int:
    """保存一条记忆"""
    conn = _connect()
    cursor = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("INSERT INTO memories (content, created_at) VALUES (?, ?)", (content, now))
    memory_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return memory_id


def get_all_memories():
    """获取所有记忆"""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT id, content, created_at FROM memories ORDER BY id DESC")
    rows = cursor.fetchall()
    conn.close()
    return [{"id": r[0], "content": r[1], "created_at": r[2]} for r in rows]


def delete_memory(memory_id: int):
    """删除某条记忆"""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
    conn.commit()
    conn.close()


# ============ 运行时状态（会话级粘性状态落库）============
def upsert_runtime_state(session_key: str, kind: str, payload: str, updated_at: float):
    """写入 / 覆盖某会话某类状态的 JSON 载荷。"""
    conn = _connect()
    conn.execute(
        """
        INSERT INTO runtime_state (session_key, kind, payload, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(session_key, kind) DO UPDATE SET
            payload = excluded.payload,
            updated_at = excluded.updated_at
        """,
        (session_key, kind, payload, updated_at),
    )
    conn.commit()
    conn.close()


def get_runtime_state(session_key: str, kind: str):
    """读取某会话某类状态，返回 (payload, updated_at)；不存在返回 None。"""
    conn = _connect()
    row = conn.execute(
        "SELECT payload, updated_at FROM runtime_state WHERE session_key = ? AND kind = ?",
        (session_key, kind),
    ).fetchone()
    conn.close()
    return (row[0], row[1]) if row else None


def delete_runtime_state(session_key: str, kind: str = None):
    """删除某会话的状态；kind 为空时删除该会话的全部状态。"""
    conn = _connect()
    if kind is None:
        conn.execute("DELETE FROM runtime_state WHERE session_key = ?", (session_key,))
    else:
        conn.execute(
            "DELETE FROM runtime_state WHERE session_key = ? AND kind = ?",
            (session_key, kind),
        )
    conn.commit()
    conn.close()


def delete_runtime_state_kind(kind: str):
    """删除某一类状态的全部记录（测试清理用）。"""
    conn = _connect()
    conn.execute("DELETE FROM runtime_state WHERE kind = ?", (kind,))
    conn.commit()
    conn.close()
