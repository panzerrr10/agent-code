import logging
import threading
import time

logger = logging.getLogger(__name__)


# ============ LLM 客户端复用 ============
# 多步 Agent 循环里每一步都会取一次客户端；若每次都 new 一个 OpenAI()，
# 就会反复创建 httpx 连接池。这里按 (api_key, base_url) 做进程内缓存，
# 键的取值只有少数几种，不会无限增长。加锁保证并发下只创建一次。
_client_cache = {}
_client_lock = threading.Lock()


def get_llm_client(api_key: str, base_url: str):
    """按 (api_key, base_url) 返回复用的 OpenAI 客户端。"""
    from openai import OpenAI

    cache_key = (api_key or "", base_url or "")
    with _client_lock:
        client = _client_cache.get(cache_key)
        if client is None:
            client = OpenAI(api_key=api_key, base_url=base_url)
            _client_cache[cache_key] = client
        return client


def call_llm_with_retry(client, max_retries=3, **kwargs):
    """
    带重试的 LLM 调用。
    - 失败重试 3 次
    - 指数退避：1s、2s、4s
    - 最后一次失败抛异常
    """
    last_error = None
    for attempt in range(max_retries):
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as e:
            last_error = e
            err_msg = str(e)

            # 不重试的错误：认证、额度、参数
            if any(
                k in err_msg
                for k in ["401", "403", "invalid_api_key", "Arrearage", "QuotaExceeded"]
            ):
                logger.error(f"不可重试错误：{err_msg[:200]}")
                raise

            if attempt < max_retries - 1:
                wait = 2**attempt
                logger.warning(
                    f"LLM 调用失败（第 {attempt + 1} 次），{wait}s 后重试：{err_msg[:100]}"
                )
                time.sleep(wait)
            else:
                logger.error(f"LLM 调用失败，已达最大重试次数：{err_msg[:200]}")

    raise last_error
