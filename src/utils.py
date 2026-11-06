import logging
import os
import threading
import time

logger = logging.getLogger(__name__)


# ============ LLM 客户端复用 ============
# 多步 Agent 循环里每一步都会取一次客户端；若每次都 new 一个 OpenAI()，
# 就会反复创建 httpx 连接池。这里按 (api_key, base_url) 做进程内缓存，
# 键的取值只有少数几种，不会无限增长。加锁保证并发下只创建一次。
_client_cache = {}
_client_lock = threading.Lock()

# 单次请求超时（秒）。SDK 默认是 **600 秒**，而且它自己还会重试 ——
# 与 call_llm_with_retry 的 3 次叠加，最坏情况是 9 次请求、十几分钟，
# 期间用户点「停止」也没有反馈（取消只在收到 chunk 时才检查）。
# 所以：显式设短超时 + max_retries=0（重试统一收敛到本模块一处）。
DEFAULT_TIMEOUT = float(os.getenv("AGENT_LLM_TIMEOUT", "120"))


def get_llm_client(api_key: str, base_url: str, timeout: float | None = None):
    """按 (api_key, base_url) 返回复用的 OpenAI 客户端。"""
    from openai import OpenAI

    cache_key = (api_key or "", base_url or "", timeout)
    with _client_lock:
        client = _client_cache.get(cache_key)
        if client is None:
            client = OpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout or DEFAULT_TIMEOUT,
                max_retries=0,          # 重试由 call_llm_with_retry 统一负责
            )
            _client_cache[cache_key] = client
        return client


def get_default_client():
    """按默认 provider 取客户端（进程内复用）。

    **收敛点**：这段逻辑原来在 agent.py / planner.py / reviewer.py 里各抄了一份。
    三份重复意味着「给客户端加超时」这种改动必然漏掉其中一两处。
    """
    from config_providers import get_provider

    cfg = get_provider()
    if not cfg.get("api_key"):
        raise RuntimeError(
            f"默认提供商「{cfg.get('name') or '（未配置）'}」还没有 api_key，"
            "请在 设置 → 模型与密钥 里添加"
        )
    return get_llm_client(cfg["api_key"], cfg["base_url"])


# 这些 HTTP 状态码**值得重试**：限流、超时、服务端临时故障。
_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
# 这些永远不要重试：认证、参数错误、模型不存在、上下文超限。
# 尤其 400 —— 原来它会被退避重试 3 次，每次重发几万 token 的请求，
# 而这类错误重试一万次也不会变好。
_UNRETRYABLE_HINTS = (
    "401", "403", "404", "400",
    "invalid_api_key", "incorrect api key", "arrearage", "quotaexceeded",
    "model not found", "does not exist",
    "context_length_exceeded", "maximum context length",
)


def _is_retryable(err: Exception) -> bool:
    status = getattr(err, "status_code", None)
    if isinstance(status, int):
        return status in _RETRYABLE_STATUS
    msg = str(err).lower()
    if any(hint in msg for hint in _UNRETRYABLE_HINTS):
        return False
    return True


def call_llm_with_retry(client, max_retries=3, **kwargs):
    """带重试的 LLM 调用。

    - 只对**值得重试**的错误重试（限流 / 超时 / 5xx / 连接错误）
    - 指数退避：1s、2s、4s
    - 认证失败、参数错误、上下文超限等直接抛，不做无谓重试
    - 最后一次失败抛异常
    """
    last_error = None
    for attempt in range(max_retries):
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as e:                               # noqa: BLE001
            last_error = e

            if not _is_retryable(e):
                logger.error("不可重试错误：%s", str(e)[:200])
                raise

            if attempt < max_retries - 1:
                wait = 2**attempt
                logger.warning(
                    "LLM 调用失败（第 %d 次），%ds 后重试：%s",
                    attempt + 1, wait, str(e)[:100],
                )
                _sleep_cancellable(wait)
            else:
                logger.error("LLM 调用失败，已达最大重试次数：%s", str(e)[:200])

    raise last_error


def _sleep_cancellable(seconds: float) -> None:
    """退避等待，但**用户点停止时要能立刻中断**。

    原来是裸的 time.sleep —— 退避期间点停止完全没反应（最长 4 秒，
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
# 为什么需要这个：多步 Agent 每一步都要把「系统提示 + 工具 schema + 代码库索引 +
# 历史」重发一遍，其中固定前缀占到单步 prompt 的一大半。各家都提供
# **前缀缓存**（命中部分通常按 1/10~1/4 计价、也明显更快），但前提是前缀
# 在两次请求之间**逐字节不变**。
#
# 问题是我们此前**根本没采集**这些字段，于是「缓存到底有没有命中」
# 完全是个盲区 —— 不知道命中率就没法判断压缩、注入、工具集变更是不是在
# 破坏前缀。这里先把「看得见」做出来。
#
# 字段名各家不一致，而且都是**可选**的：
#   DeepSeek  : prompt_cache_hit_tokens / prompt_cache_miss_tokens
#   OpenAI    : prompt_tokens_details.cached_tokens
#   百炼/智谱 : 可能一个都没有
# 所以全程用 getattr 兜住，取不到就返回 0，绝不因为缺字段把主流程搞崩。


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
    两个都为 0 表示「该厂商没上报」，与「命中率 0%」是**两回事** ——
    调用方要据此区分「没数据」和「真的没命中」。
    """
    if usage is None:
        return {"cache_hit_tokens": 0, "cache_miss_tokens": 0}

    hit_raw = _get_any(usage, "prompt_cache_hit_tokens")
    miss_raw = _get_any(usage, "prompt_cache_miss_tokens")

    # OpenAI 形状：prompt_tokens_details.cached_tokens
    if hit_raw is None:
        details = _get_any(usage, "prompt_tokens_details")
        hit_raw = _get_any(details, "cached_tokens")

    # 两个字段都没有 → 这家厂商不上报缓存信息。
    # 这里必须**直接返回 0**，不能退化成「miss = prompt_tokens」——
    # 那等于把「没有数据」谎报成「命中率 0%」，会把优化方向带偏。
    if hit_raw is None and miss_raw is None:
        return {"cache_hit_tokens": 0, "cache_miss_tokens": 0}

    try:
        hit = max(int(hit_raw or 0), 0)
    except (TypeError, ValueError):
        hit = 0

    if miss_raw is None:
        # 只报 hit 的厂商：miss 用 prompt - hit 推出来
        # （DeepSeek/OpenAI 的 prompt_tokens 都包含 cached 部分）
        try:
            prompt_tokens = int(_get_any(usage, "prompt_tokens") or 0)
        except (TypeError, ValueError):
            prompt_tokens = 0
        miss = max(prompt_tokens - hit, 0)
    else:
        try:
            miss = max(int(miss_raw), 0)
        except (TypeError, ValueError):
            miss = 0
    return {"cache_hit_tokens": hit, "cache_miss_tokens": miss}


def cache_hit_rate(hit_tokens: int, prompt_tokens: int):
    """命中率（0.0~1.0）。prompt_tokens 为 0 时返回 ``None``（表示「没有数据」）。

    注意分母用 ``prompt_tokens`` 而不是 ``hit + miss`` —— 后者在厂商只报
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
