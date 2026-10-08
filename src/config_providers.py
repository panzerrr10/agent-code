"""多提供商（Provider）配置中心。

本模块集中管理后端写死的 4 家大模型提供商的元数据：
DeepSeek / 阿里云百炼 / 智谱 AI / OpenAI。

设计说明：
- api_key 一律从环境变量读取，**不写入源码、不进 git**。
  真实 Key 放在 .env 中（.env 已被 .gitignore 忽略）。
- 前端只拿 ``get_provider_list()``（不含 api_key），
  绝不下发密钥到浏览器。
"""

# 模型名以各家官方文档为准；若你开通的模型名不同，直接改下面 PROVIDERS 的 models。

import logging
import os

from dotenv import load_dotenv

# 本模块在 import 时就用 os.getenv 构建 PROVIDERS，因此必须自己先加载 .env。
# 否则「谁先 import」会决定密钥是否读得到——import 顺序一变密钥就全空，非常脆弱。
load_dotenv()

logger = logging.getLogger(__name__)

PROVIDERS = {
    "deepseek": {
        "name": "DeepSeek", "icon": "🐳",
        "api_key": os.getenv("DEEPSEEK_API_KEY", ""),
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "default_model": "deepseek-chat",
        "supports_embedding": False,
    },
    "bailian": {
        "name": "阿里云百炼", "icon": "☁️",
        "api_key": os.getenv("DASHSCOPE_API_KEY", ""),
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-max", "qwen-plus", "qwen-turbo"],
        "default_model": "qwen-plus",
        "supports_embedding": True,
        "embedding_model": "text-embedding-v4",
    },
    "zhipu": {
        "name": "智谱 AI", "icon": "🧠",
        "api_key": os.getenv("ZHIPU_API_KEY", ""),
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-4-plus", "glm-4", "glm-4-flash"],
        "default_model": "glm-4-plus",
        "supports_embedding": True,
        "embedding_model": "embedding-3",
    },
    "openai": {
        "name": "OpenAI", "icon": "🤖",
        "api_key": os.getenv("OPENAI_API_KEY", ""),
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo"],
        "default_model": "gpt-4o-mini",
        "supports_embedding": True,
        "embedding_model": "text-embedding-3-small",
    },
}

DEFAULT_PROVIDER = "deepseek"
RAG_PROVIDER = "bailian"   # RAG 专用的 embedding provider（锁死阿里云百炼）


def get_provider(provider_id: str) -> dict:
    """返回指定 provider 的完整配置（含 api_key / base_url / default_model）。

    找不到 provider_id 时回退到 DEFAULT_PROVIDER。
    api_key 为空时不抛异常（允许运行时为空），仅打一条 warning 日志。
    """
    provider = PROVIDERS.get(provider_id)
    if provider is None:
        logger.warning("未知的 provider_id=%r，回退到默认 provider=%r", provider_id, DEFAULT_PROVIDER)
        provider_id = DEFAULT_PROVIDER
        provider = PROVIDERS[DEFAULT_PROVIDER]

    if not provider.get("api_key"):
        logger.warning(
            "provider=%r 的 api_key 为空，请检查环境变量是否已配置（见 .env）",
            provider_id,
        )

    return provider


def get_provider_list() -> list:
    """返回给前端使用的 provider 列表，**不含 api_key**。

    每项包含：id, name, icon, models, default_model, supports_embedding, has_key。
    has_key 用于前端判断该 provider 是否已配置密钥（未配置时显示灰色标记）。
    """
    result = []
    placeholders = ["你的", "your", "placeholder"]
    for pid, p in PROVIDERS.items():
        key = p.get("api_key", "")
        has_key = bool(key) and not any(
            kw.lower() in key.lower() for kw in placeholders
        )
        result.append({
            "id": pid,
            "name": p["name"],
            "icon": p["icon"],
            "models": p["models"],
            "default_model": p["default_model"],
            "supports_embedding": p.get("supports_embedding", False),
            "has_key": has_key,
        })
    return result


def get_provider_id_by_base_url(base_url: str) -> str:
    """根据 base_url 反查 provider id。

    用于 CLI 等未显式传 provider_id 的场景，避免把某家专属参数
    （如 DeepSeek 的 enable_search）误发给其他厂商。
    """
    if not base_url:
        return DEFAULT_PROVIDER
    for pid, p in PROVIDERS.items():
        url = p.get("base_url") or ""
        if url and url in base_url:
            return pid
    return DEFAULT_PROVIDER


def get_extra_body(provider_id: str) -> dict:
    """返回该 provider 所需的额外请求体参数（extra_body）。

    只有 DeepSeek 支持 ``enable_search`` 联网搜索开关；其他厂商收到未知参数
    会直接报 400，因此必须按 provider 条件化下发，不能全局写死。
    provider_id 未知时返回空 dict，绝不猜测。
    """
    provider = PROVIDERS.get(provider_id)
    if provider is None:
        return {}
    base_url = provider.get("base_url") or ""
    if provider_id == "deepseek" or "deepseek" in base_url:
        return {
            "enable_search": True,
            "search_options": {"search_strategy": "turbo"},
        }
    return {}


def get_rag_config() -> dict:
    """返回 RAG 专用配置（含 api_key / base_url / embedding_model）。

    默认使用 RAG_PROVIDER（阿里云百炼）。
    若 .env 中设置了 RAG_API_KEY / RAG_BASE_URL，则作为独立覆盖优先使用。
    """
    config = dict(get_provider(RAG_PROVIDER))

    override_key = os.getenv("RAG_API_KEY")
    if override_key:
        config["api_key"] = override_key

    override_base_url = os.getenv("RAG_BASE_URL")
    if override_base_url:
        config["base_url"] = override_base_url

    return config
