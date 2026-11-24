"""持久化层：MySQL（SQLAlchemy 2.0 ORM）。

## 为什么是 MySQL 而不是 SQLite 文件

原先会话、消息、断点状态都写在 `data/chat.db` 里，换机器 / 多端访问 / 多人共用时
根本拿不到同一份历史。现在统一走 MySQL：同一份会话记录可以被 Web、桌面、CLI
三个入口共享，也不再产生「本地残留了一个 chat.db」这种问题。

## 单元测试怎么办

单测不应该依赖一个跑着的 MySQL。因此连接串的解析顺序是：

1. ``AGENT_DB_URL``（显式指定，生产用这个）
2. ``AGENT_DB_PATH``（仅测试：转成 ``sqlite:///...`` 临时文件）
3. 都没有 → 用 ``config.mysql_url()`` 拼出来的 MySQL 连接串

生产环境永远走第 3 条或第 1 条；第 2 条只是为了让 804 个单测保持隔离与秒级速度。
"""

from .engine import (
    get_engine,
    get_session,
    get_session_factory,
    init_schema,
    reset_engine,
    resolve_db_url,
)
from .models import (
    Base,
    Document,
    Memory,
    Message,
    RunChange,
    RuntimeState,
    Workspace,
)
from .models import (
    Session as SessionModel,
)

__all__ = [
    "Base",
    "Document",
    "Memory",
    "Message",
    "RunChange",
    "RuntimeState",
    "SessionModel",
    "Workspace",
    "get_engine",
    "get_session",
    "get_session_factory",
    "init_schema",
    "reset_engine",
    "resolve_db_url",
]
