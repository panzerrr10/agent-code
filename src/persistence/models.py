"""ORM 模型（MySQL 8 / utf8mb4）。

设计要点：

1. **大字段用 LONGTEXT**。消息内容里可能有整份文档正文、图片 data URL，
   MySQL 的 ``TEXT`` 只有 64KB，必然截断 —— 所以统一用 ``LONGTEXT``，
   在 SQLite（测试）下退化为普通 ``TEXT``。
2. **时间统一存 UTC 字符串**（``YYYY-MM-DD HH:MM:SS``）。原实现存的是
   ``datetime.now().strftime(...)`` 的本地时间，迁移时保持同样的可读格式，
   避免前端解析逻辑一起改。
3. **索引覆盖热路径**：``messages.session_id``、``sessions.updated_at``、
   ``run_changes(session_id, run_index)`` —— 这三条是列表页与「第 N 轮改动」的查询入口。
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# SQLite 下没有 LONGTEXT，用 TEXT 顶替（单测用）
LONGTEXT_TYPE = Text().with_variant(LONGTEXT(), "mysql")

# 主键/外键类型：MySQL 用 BIGINT，SQLite 必须退化为 INTEGER。
# 原因：SQLite 只有 ``INTEGER PRIMARY KEY`` 才是自增行号，写 BIGINT 会导致
# INSERT 时 id 为 NULL（"NOT NULL constraint failed: workspaces.id"）。
ID_TYPE = BigInteger().with_variant(Integer(), "sqlite")


class Base(DeclarativeBase):
    """所有表的基类。"""


class Workspace(Base):
    """工作区：一组会话 + 一个工作目录。"""

    __tablename__ = "workspaces"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    # 该工作区打开过的文件标签（JSON 数组字符串），切工作区时恢复用
    open_files: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    last_used_at: Mapped[str | None] = mapped_column(String(32), nullable=True)


class Session(Base):
    """会话。权限等级存在服务端，前端传什么都不作数。"""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    workspace_id: Mapped[int | None] = mapped_column(ID_TYPE, nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="新对话")
    # 1 只读 / 2 确认 / 3 完全
    permission_level: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)


class Message(Base):
    """一条消息。

    ``content`` 对 user 可能是纯文本或 ``{"text":..., "images":[...]}`` JSON；
    对 assistant 是 ``{"answer":..., "tools":[...], "tokens":{...}}`` JSON。
    保持与原实现一致的格式，前端解析逻辑不用改。
    """

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(ID_TYPE, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(LONGTEXT_TYPE, nullable=False)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (Index("ix_messages_session_id", "session_id"),)


class Document(Base):
    """知识库文档（向量在 Chroma，元数据在这里）。"""

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)


class Memory(Base):
    """跨会话长期记忆。"""

    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)


class RuntimeState(Base):
    """按会话分桶的运行时状态（断点 / 计划 / 项目根目录）。

    ``payload`` 是 JSON 字符串；``updated_at`` 是 epoch 秒（浮点），
    因为 pending 有 24h TTL，需要做数值比较。
    """

    __tablename__ = "runtime_state"

    session_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    payload: Mapped[str] = mapped_column(LONGTEXT_TYPE, nullable=False)
    updated_at: Mapped[float] = mapped_column(nullable=False)

    __table_args__ = (UniqueConstraint("session_key", "kind", name="uq_runtime_state"),)


class RunChange(Base):
    """「第 N 轮改动」记录（带本轮快照 diff）。"""

    __tablename__ = "run_changes"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(ID_TYPE, nullable=False)
    run_index: Mapped[int] = mapped_column(Integer, nullable=False)
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    change_type: Mapped[str] = mapped_column(String(16), nullable=False)
    tool: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    diff: Mapped[str | None] = mapped_column(LONGTEXT_TYPE, nullable=True)
    added: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    removed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    truncated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        Index("ix_run_changes_session_run", "session_id", "run_index"),
    )
