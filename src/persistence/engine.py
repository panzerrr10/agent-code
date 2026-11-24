"""数据库引擎与会话工厂。

连接串解析优先级见包文档。这里额外处理两件事：

1. **引擎按 URL 缓存并自动重建**：单测会在不同用例里切换 ``AGENT_DB_PATH``，
   如果只缓存第一次的引擎，后面的用例会连到前一个临时库上（串数据）。
   因此每次取引擎都比对一次「解析出来的 URL」与「当前引擎的 URL」，不同就重建。
2. **MySQL 连接池参数**：``pool_pre_ping`` 防掉线，``pool_recycle=1800`` 躲开
   MySQL 默认 8 小时的 ``wait_timeout``（长任务里隔几小时再查库是最常见的坑）。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)

_engine: Engine | None = None
_engine_url: str | None = None
_SessionLocal: sessionmaker | None = None


def resolve_db_url() -> str:
    """解析当前应使用的数据库连接串。"""
    explicit = os.getenv("AGENT_DB_URL")
    if explicit:
        return explicit.strip()

    # 仅测试：给一个临时 SQLite 文件，保证用例之间完全隔离
    sqlite_path = os.getenv("AGENT_DB_PATH")
    if sqlite_path:
        return f"sqlite:///{sqlite_path}"

    import config

    return config.mysql_url()


def get_engine() -> Engine:
    """取（必要时重建）全局引擎。"""
    global _engine, _engine_url, _SessionLocal

    url = resolve_db_url()
    if _engine is not None and _engine_url == url:
        return _engine

    kwargs: dict = {"future": True}
    if url.startswith("mysql"):
        kwargs.update(
            pool_pre_ping=True,
            pool_recycle=1800,
            pool_size=10,
            max_overflow=20,
            connect_args={"charset": "utf8mb4"},
        )
    else:
        # SQLite：允许跨线程（Flask 的 SSE 生成器在别的线程里跑）
        kwargs.update(connect_args={"check_same_thread": False})

    _engine = create_engine(url, **kwargs)
    _engine_url = url
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    logger.info("数据库已连接：%s", _safe_url(url))
    return _engine


def _safe_url(url: str) -> str:
    """日志里隐去密码。"""
    if "@" in url and "//" in url:
        head, tail = url.split("//", 1)
        if "@" in tail:
            cred, host = tail.split("@", 1)
            user = cred.split(":")[0]
            return f"{head}//{user}:***@{host}"
    return url


def get_session_factory() -> sessionmaker:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


@contextmanager
def get_session() -> Iterator[Session]:
    """事务边界：正常提交，异常回滚，最后一定关闭。"""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_schema() -> None:
    """建表（幂等）。生产用 Alembic 迁移；这里保证首次启动能直接跑起来。"""
    from .models import Base

    Base.metadata.create_all(get_engine())


def reset_engine() -> None:
    """测试用：丢弃当前引擎，下次按新 URL 重建。"""
    global _engine, _engine_url, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _engine_url = None
    _SessionLocal = None
