"""通用工具：prompt 缓存统计、JSON 提取、可取消的退避等待。

## 重构说明

原来这里还有 ``get_llm_client`` / ``call_llm_with_retry`` —— 直接用 openai SDK
建客户端并手工重试。现在模型调用统一走 LangChain（见 ``llm.py``）：
``ChatOpenAI`` 自带连接池复用，重试由 ``llm.build_chat_model`` 的
``max_retries`` 与调用方的错误分类共同负责，这一层不再需要。

留下的都是与框架无关的纯函数，被 agent / 图 / 辅助角色共用。
"""

from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = float(os.getenv("AGENT_LLM_TIMEOUT", "120"))


def sleep_cancellable(seconds: float) -> None:
    """退避等待，但**用户点停止时要能立刻中断**。

    裸的 ``time.sleep`` 在退避期间点停止完全没反应（最长 4 秒，
    加上前面重试的网络等待，体感像卡死）。这里分片等待并轮询取消标志。
    """
    try:
        import runtime

        event = runtime.get_cancel_event()
    except Exception:                                        # noqa: BLE001
        event = None

    if event is None:
        time.sleep(seconds)
        return

    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or event.is_set():
            return
        if event.wait(min(0.2, remaining)):
            return


# ============ prompt 缓存统计 ============
#
# 多步 Agent 每一步都要把「系统提示 + 工具 schema + 代码库索引 + 历史」重发一遍，
# 其中固定前缀占单步 prompt 的一大半。各家都提供**前缀缓存**（命中部分通常按
# 1/10~1/4 计价、也明显更快），但前提是前缀在两次请求之间**逐字节不变**。
# 采集这些字段，才能判断压缩、注入、工具集变更是不是在破坏前缀。


def _get_any(obj, *names):
    """从对象（或 dict）里按顺序取第一个存在的字段。"""
    for name in names:
        if obj is None:
            return None
        if isinstance(obj, dict):
            value = obj.get(name)
        else:
            value = getattr(obj, name, None)
        if value is not None:
            return value
    return None


def extract_cache_stats(usage) -> dict:
    """从 provider 的 ``usage`` 里提取缓存命中统计。

    返回 ``{"cache_hit_tokens": int, "cache_miss_tokens": int}``。
    两个都为 0 表示「该厂商没上报」，与「命中率 0%」是**两回事**。
    """
    if usage is None:
        return {"cache_hit_tokens": 0, "cache_miss_tokens": 0}

    # DeepSeek：prompt_cache_hit_tokens / prompt_cache_miss_tokens
    hit = _get_any(usage, "prompt_cache_hit_tokens")
    miss = _get_any(usage, "prompt_cache_miss_tokens")
    if hit is not None or miss is not None:
        prompt = int(_get_any(usage, "prompt_tokens") or 0)
        hit_i = int(hit or 0)
        # 只报 hit 的厂商（DeepSeek 在某些版本下如此）用 prompt - hit 推出 miss，
        # 否则会把「没上报」误当成「全都没命中」。
        miss_i = int(miss) if miss is not None else max(prompt - hit_i, 0)
        return {"cache_hit_tokens": hit_i, "cache_miss_tokens": miss_i}

    # OpenAI：prompt_tokens_details.cached_tokens
    details = _get_any(usage, "prompt_tokens_details")
    if details is not None:
        cached = _get_any(details, "cached_tokens")
        if cached is not None:
            prompt = int(_get_any(usage, "prompt_tokens") or 0)
            return {
                "cache_hit_tokens": int(cached or 0),
                "cache_miss_tokens": max(prompt - int(cached or 0), 0),
            }

    return {"cache_hit_tokens": 0, "cache_miss_tokens": 0}


def cache_hit_rate(hit_tokens: int, prompt_tokens: int):
    """命中率（0.0~1.0）。prompt_tokens 为 0 时返回 ``None``（表示「没有数据」）。

    分母用 ``prompt_tokens`` 而不是 ``hit + miss`` —— 后者在厂商只报
    一个字段时会被算成 100%，是个很容易骗过自己的错误口径。
    """
    try:
        hit_tokens = int(hit_tokens or 0)
        prompt_tokens = int(prompt_tokens or 0)
    except (TypeError, ValueError):
        return None
    if prompt_tokens <= 0:
        return None
    return max(0.0, min(1.0, hit_tokens / prompt_tokens))


# ============ 模型输出的 JSON 提取 ============
# Planner 与 Reviewer 都要从模型回答里抠 JSON（模型经常包一层 ```json
# 或者前后带一句废话）。两个模块原先各抄了一份，这里收敛成一处。


def extract_json(content: str) -> dict | None:
    """从模型输出里抠出 JSON（容忍 ```json 包裹和前后废话）。

    Args:
        content: 模型返回的原始文本。

    Returns:
        解析出的 dict；无法解析时返回 ``None``（不抛异常）。
    """
    import json

    if not content:
        return None

    text = content.strip()
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) >= 2:
            text = parts[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
    try:
        return json.loads(text)
    except Exception:                                        # noqa: BLE001
        pass

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:                                    # noqa: BLE001
            return None
    return None
