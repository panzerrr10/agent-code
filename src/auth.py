"""最小鉴权：给本机 HTTP API 加一道共享令牌（shared secret）门。

**为什么需要**：应用只监听 127.0.0.1，但
1. 浏览器里的第三方页面仍可发起 CSRF（例如伪造 `POST /run`、`DELETE /sessions/1`）；
2. 同机其它进程、以及共享机器上的其它用户，也能直接打这些接口。

**做法**：所有接口都要求自定义请求头 ``X-Auth-Token``。
跨域页面无法在「简单请求」里设置自定义头，带自定义头的请求会先触发预检，
而预检同样会因为缺少令牌被拒 —— CSRF 路径由此切断。

**令牌来源**（优先级从高到低）：
1. 环境变量 ``AGENT_AUTH_TOKEN``（方便脚本 / curl 复用）
2. ``data/auth_token`` 文件：自动生成，权限 0600，重启后保持不变
3. 文件不可写时退化为进程内随机令牌（重启即失效，会打 warning）

**范围说明**：这解决的是「跨域 CSRF」与「同机其它用户」。
它挡不住「与进程同 uid 的本地进程」——那种情况下对方可以直接读令牌文件，
这属于本地可信边界之外，需要靠操作系统权限而非应用层解决。
"""

from __future__ import annotations

import logging
import os
import secrets

from paths import DATA_DIR

logger = logging.getLogger(__name__)

HEADER_NAME = "X-Auth-Token"
TOKEN_FILE = os.path.join(DATA_DIR, "auth_token")

# 免鉴权路径：页面外壳与静态资源。
# 页面 HTML 里会注入令牌，因此必须允许匿名获取；
# 跨域页面读不到该响应内容（无 CORS 头），所以不会因此泄露令牌。
PUBLIC_EXACT = {"/", "/favicon.ico"}
PUBLIC_PREFIX = ("/static/",)

_token: str | None = None


def _load_or_create() -> str:
    env_token = os.getenv("AGENT_AUTH_TOKEN")
    if env_token and env_token.strip():
        logger.info("使用环境变量 AGENT_AUTH_TOKEN 作为访问令牌")
        return env_token.strip()

    try:
        if os.path.exists(TOKEN_FILE):
            with open(TOKEN_FILE, encoding="utf-8") as f:
                saved = f.read().strip()
            if saved:
                return saved

        token = secrets.token_urlsafe(32)
        # 以 0600 创建：避免生成过程中被同机其它用户读到
        fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token)
        logger.info("已生成访问令牌并保存到 %s", TOKEN_FILE)
        return token
    except Exception as e:  # pragma: no cover - 依赖文件系统
        logger.warning("令牌文件不可用（%s），本次改用进程内随机令牌：%s", TOKEN_FILE, e)
        return secrets.token_urlsafe(32)


def get_token() -> str:
    global _token
    if _token is None:
        _token = _load_or_create()
    return _token


def verify(token: str | None) -> bool:
    """常数时间比较，避免时序侧信道。"""
    if not token:
        return False
    return secrets.compare_digest(str(token), get_token())


def is_public_path(path: str) -> bool:
    return path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIX)


def reset_cache() -> None:
    """测试用：丢弃进程内缓存，下次调用重新读取。"""
    global _token
    _token = None
