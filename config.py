"""全局运行参数（批次 6 精简版）。

API 相关配置（Key / Base URL / 模型名）已全部迁到 config_providers.py，
本模块只保留 Agent 的通用运行参数。
"""

from dotenv import load_dotenv

# ✅ 加载 .env 文件
load_dotenv()

from paths import BASE_DIR, is_allowed_path

# ============ Agent 配置 ============
# （批次 6 起：DASHSCOPE_API_KEY / BASE_URL / MODEL / PLANNER_MODEL /
#   EXECUTOR_MODEL / REVIEWER_MODEL 已删除，统一由 config_providers.py 提供）
MAX_STEPS = 50  # 最多执行 50步，防止死循环
MAX_FILE_SIZE = 100_000  # 单文件最多读 100KB
MAX_LIST_ITEMS = 200  # 单次最多列 200 项



