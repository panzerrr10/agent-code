"""SQLite 存储层（会话 / 消息 / 文档 / 长期记忆）。

设计说明：
- 统一通过 ``_connect()`` 建连：开启 WAL 提升读写并发，设置 busy_timeout
  避免多个请求同时写库时直接抛 "database is locked"。
- 对最热的查询路径建索引（messages.session_id、sessions.updated_at）。
"""

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

    # 索引：读会话历史是最热路径（WHERE session_id ORDER BY id）
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_documents_created ON documents(created_at DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_runtime_state_kind ON runtime_state(kind)"
    )

    conn.commit()
    conn.close()


def create_session(title: str = "新对话") -> int:
    """创建新会话，返回 session_id"""
    conn = _connect()
    cursor = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        "INSERT INTO sessions (title, created_at, updated_at) VALUES (?, ?, ?)",
        (title, now, now),
    )
    session_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return session_id


def get_all_sessions():
    """获取所有会话，按更新时间倒序"""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, title, created_at, updated_at FROM sessions ORDER BY updated_at DESC"
    )
    rows = cursor.fetchall()
    conn.close()
    return [{"id": r[0], "title": r[1], "created_at": r[2], "updated_at": r[3]} for r in rows]


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
