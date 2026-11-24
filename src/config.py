"""全局运行参数。

API 相关配置（Key / Base URL / 模型名）在 ``config_providers.py``（用户界面里定义）。
本模块只放「进程级」参数：步数上限、上下文预算、以及**数据库连接**。
"""

import os

from dotenv import load_dotenv

load_dotenv()


# ============ Agent 配置 ============
MAX_STEPS = 50  # 最多执行 50 步，防止死循环
MAX_FILE_SIZE = 100_000  # 单文件最多读 100KB
MAX_LIST_ITEMS = 200  # 单次最多列 200 项

# 会话权限等级：1 只读 / 2 确认 / 3 完全。新会话一律从最稳妥的 2 开始。
DEFAULT_PERMISSION_LEVEL = 2

# ============ 上下文压缩 ============
# 单次运行的上下文预算（tokens）。超过就压：先剪枝旧工具结果，再折叠旧步骤。
CONTEXT_BUDGET_TOKENS = 32000
CONTEXT_KEEP_RECENT_TOOL_RESULTS = 8


# ============ 数据库（MySQL）============
# 会话、消息、断点状态全部落在 MySQL，不再使用本地 chat.db 文件。
# 生产用 AGENT_DB_URL 直接给完整连接串；也可以用下面这些分项配置拼。
MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "agent")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "agent_code")


def mysql_url() -> str:
    """拼出 SQLAlchemy 的 MySQL 连接串。

    密码里可能含 ``@`` / ``:`` 等字符，必须做 URL 转义，否则连接串会被解析错
    （这类故障报的是「Unknown MySQL server host」，很容易误导排查方向）。
    """
    from urllib.parse import quote_plus

    user = quote_plus(MYSQL_USER)
    password = quote_plus(MYSQL_PASSWORD)
    auth = f"{user}:{password}" if password else user
    return (
        f"mysql+pymysql://{auth}@{MYSQL_HOST}:{MYSQL_PORT}"
        f"/{MYSQL_DATABASE}?charset=utf8mb4"
    )
