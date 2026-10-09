"""全局运行参数（批次 6 精简版）。

API 相关配置（Key / Base URL / 模型名）已全部迁到 config_providers.py，
本模块只保留 Agent 的通用运行参数。
"""

from dotenv import load_dotenv

# ✅ 加载 .env 文件
load_dotenv()


# ============ Agent 配置 ============
# （批次 6 起：DASHSCOPE_API_KEY / BASE_URL / MODEL / PLANNER_MODEL /
#   EXECUTOR_MODEL / REVIEWER_MODEL 已删除，统一由 config_providers.py 提供）
MAX_STEPS = 50  # 最多执行 50步，防止死循环
MAX_FILE_SIZE = 100_000  # 单文件最多读 100KB
MAX_LIST_ITEMS = 200  # 单次最多列 200 项

# 会话权限等级：1 只读 / 2 确认 / 3 完全。新会话一律从最稳妥的 2 开始。
DEFAULT_PERMISSION_LEVEL = 2

# ============ 上下文压缩 ============
# 单次运行的上下文预算（tokens）。超过就压：先剪枝旧工具结果，再折叠旧步骤。
# 取 32K：约 DeepSeek 64K 窗口的一半，给工具 schema、输出和估算误差留足余量。
CONTEXT_BUDGET_TOKENS = 32000
# 保留最近多少条工具结果的全文（更旧的会被换成一行占位）
CONTEXT_KEEP_RECENT_TOOL_RESULTS = 8



