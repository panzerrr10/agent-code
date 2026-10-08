"""MCP 客户端：把外部 MCP server 的工具接进 agent 的工具系统。

## 为什么要这么写

**MCP SDK 是 async 的，而 agent 主循环是同步生成器。** 这个模块的全部复杂度都来自
这一个事实。三种桥接方式的取舍：

- 每次调用 ``asyncio.run()``：每次重启事件循环与子进程 → 慢、丢服务器状态。✗
- 把整个 agent 循环改成 async：牵连 Flask/SSE/多 Agent，成本极高。✗
- **一个常驻事件循环线程 + 长连接会话**（本实现）：✓

## 每个 server 一个常驻 Task

``AsyncExitStack`` 与 anyio 的取消作用域是**绑定任务**的：在一个任务里 enter、
在另一个任务里 exit 会报错。所以每个 server 都跑在自己的长驻协程里：

    async with AsyncExitStack() as stack:      # enter / exit 都在这个任务里
        read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        ...
        await stop_event.wait()                # 停在这里常驻

外部通过 ``run_coroutine_threadsafe`` 提交调用，用 ``.result(timeout)`` 取结果。

## 安全要点

- **工具名强制命名空间** ``mcp__<server>__<tool>``，且用注册表反查，
  避免第三方 server 用 ``write_file`` 这种名字**劫持内置工具**。
- 环境变量**白名单传递**（见 mcp_config.server_env），不把 .env 里的密钥交给第三方进程。
- **联网由外层控制**：本模块只管起进程；是否允许联网由调用方按 server 配置决定
  （宿主机执行模式下无法用 Docker 断网，只能靠用户逐 server 授权 + 知情）。
"""

from __future__ import annotations

import asyncio
import atexit
import hashlib
import logging
import os
import tempfile
import threading
import time

import mcp_config

logger = logging.getLogger(__name__)

MCP_PREFIX = "mcp__"
_MAX_TOOL_NAME = 64          # OpenAI function name 上限

_state = {
    "loop": None,
    "thread": None,
    "servers": {},           # name -> _Server
    # full_name -> {"server": 名, "kind": tool|resource|prompt, "name": 原名}
    "registry": {},
    "lock": threading.RLock(),
}

# resources / prompts 是以「合成工具」的形式暴露给模型的：MCP 的 resources 和
# prompts 本来不是工具，但对模型来说「列出来、按 uri 读」就是一个动作。
RES_LIST = "list_resources"
RES_READ = "read_resource"
PROMPT_LIST = "list_prompts"
PROMPT_GET = "get_prompt"


# ---------------------------------------------------------------- 命名空间


def make_full_name(server: str, tool: str) -> str:
    """``mcp__<server>__<tool>``，并保证符合 OpenAI 的函数名格式与长度上限。"""
    prefix = f"{MCP_PREFIX}{server}__"
    safe = "".join(c if (c.isalnum() or c in "_-") else "_" for c in tool)
    room = _MAX_TOOL_NAME - len(prefix)
    if len(safe) <= room:
        return prefix + safe
    # 超长就截断 + 短哈希，既不超限也不会撞名
    digest = hashlib.sha1(tool.encode("utf-8")).hexdigest()[:6]
    return prefix + safe[: max(1, room - 7)] + "_" + digest


def is_mcp_tool(name: str) -> bool:
    return isinstance(name, str) and name.startswith(MCP_PREFIX)


def resolve(full_name: str):
    """反查注册信息。用注册表而不是解析字符串 —— server 名里允许下划线。"""
    return _state["registry"].get(full_name)


def registry_snapshot() -> list:
    """注册表快照（含 full_name / server / kind / name），供上层展示用。"""
    return [dict(entry, full_name=full) for full, entry in _state["registry"].items()]


def resolve_tool(full_name: str):
    """兼容旧调用：返回 (server, 原名)。"""
    e = _state["registry"].get(full_name)
    return (e["server"], e["name"]) if e else None


# ---------------------------------------------------------------- 事件循环


def _ensure_loop() -> asyncio.AbstractEventLoop:
    loop = _state["loop"]
    if loop is not None:
        return loop
    with _state["lock"]:
        if _state["loop"] is not None:
            return _state["loop"]
        loop = asyncio.new_event_loop()

        def _run():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        t = threading.Thread(target=_run, name="mcp-loop", daemon=True)
        t.start()
        _state["loop"] = loop
        _state["thread"] = t
        logger.info("MCP 事件循环线程已启动")
    return loop


def _on_loop_thread() -> bool:
    return threading.current_thread() is _state["thread"]


# ---------------------------------------------------------------- 沙箱（二期）


def _docker_stdio_args(cfg: dict) -> list[str]:
    """把 stdio server 关进 Docker。

    与 ``sandbox.py`` 用同一套硬化参数，但这里是**长驻进程**，所以：
    - ``-i`` 必须留（MCP 的 stdio 就是靠 stdin/stdout 通信的）
    - ``--init`` 帮忙回收僵尸子进程
    - **不注入宿主的环境变量白名单**：容器里有自己的 PATH 等，
      把宿主的传进去反而会指向容器里不存在的路径
    """
    import sandbox as sb

    args = ["run", "--rm", "-i", "--init"]
    # 联网必须逐 server 显式授权；没授权就彻底断网
    args += ["--network", "bridge" if cfg.get("allow_network") else "none"]
    args += ["--memory", sb.MEMORY, "--cpus", sb.CPUS, "--pids-limit", "256"]
    args += ["--cap-drop", "ALL", "--security-opt", "no-new-privileges"]
    args += ["--user", f"{os.getuid()}:{os.getgid()}"]
    args += ["-e", "HOME=/tmp"]
    for m in cfg.get("sandbox_mounts") or []:
        args += ["-v", f"{m}:{m}"]          # 挂到同路径，插件配置里的绝对路径才不用改
    for k, v in (cfg.get("env") or {}).items():
        args += ["-e", f"{k}={v}"]
    args += ["-w", "/tmp", cfg["sandbox_image"], cfg["command"], *cfg["args"]]
    return args


def build_stdio_command(cfg: dict) -> tuple[str, list[str]]:
    """返回 (可执行文件, 参数)。开了沙箱就套一层 docker run。"""
    if not cfg.get("sandbox"):
        return cfg["command"], list(cfg["args"])
    return "docker", _docker_stdio_args(cfg)


class _ErrLog:
    """把子进程 stderr 落到临时文件，失败时读回来。

    **不能用 StringIO 之类**：SDK 会把 errlog 的 fileno() 直接交给子进程当 stderr，
    所以必须是真实文件。没有它的话，子进程起不来时只能看到「Connection closed」，
    完全不知道是命令不存在、镜像缺依赖还是权限问题。
    """

    def __init__(self):
        fd, self.path = tempfile.mkstemp(prefix="mcp-err-", suffix=".log")
        os.close(fd)
        self._fh = open(self.path, "w+", encoding="utf-8", errors="ignore", buffering=1)

    @property
    def file(self):
        return self._fh

    def tail(self, max_chars: int = 300) -> str:
        try:
            self._fh.flush()
            with open(self.path, encoding="utf-8", errors="ignore") as f:
                lines = [ln.strip() for ln in f if ln.strip()]
        except OSError:
            return ""
        return " | ".join(lines[-4:])[:max_chars]

    def close(self):
        try:
            self._fh.close()
        except OSError:
            pass
        try:
            os.unlink(self.path)
        except OSError:
            pass


def _describe_error(e: BaseException) -> str:
    """把 SDK 抛的异常变成能看懂的一句话。

    MCP SDK 会把子进程启动失败包在 ExceptionGroup 里，直接 str() 只得到
    「unhandled errors in a TaskGroup (1 sub-exception)」—— 对用户毫无用处。
    这里逐层剥开，取最内层真正的错误。
    """
    seen = 0
    while isinstance(e, BaseExceptionGroup) and e.exceptions and seen < 8:
        e = e.exceptions[0]
        seen += 1
    text = str(e) or type(e).__name__
    return text[:400]


class _Server:
    """一个 MCP server 的运行时状态。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.name = cfg["name"]
        self.status = "idle"          # idle | connecting | ready | error | stopped
        self.error = ""
        self.tools = []               # list[mcp.types.Tool]
        self.errlog = None            # 子进程 stderr 缓冲
        self.restart_count = 0        # 看门狗自动重连了几次
        self.resources = []           # list[mcp.types.Resource]
        self.prompts = []             # list[mcp.types.Prompt]
        self.session = None
        self.stop_event = None        # asyncio.Event（在 loop 线程里创建）
        self.future = None            # concurrent.futures.Future

    @property
    def timeout(self) -> int:
        return int(self.cfg.get("timeout") or 30)

    async def _run(self, ready: threading.Event):
        from contextlib import AsyncExitStack

        from mcp import ClientSession

        self.stop_event = asyncio.Event()
        try:
            cfg = self.cfg
            transport = cfg["transport"]
            if transport == "stdio":
                from mcp import StdioServerParameters
                from mcp.client.stdio import stdio_client

                cmd, args = build_stdio_command(cfg)
                params = StdioServerParameters(
                    command=cmd, args=args,
                    env=mcp_config.server_env(cfg), cwd=cfg["cwd"] or None,
                )
                self.errlog = _ErrLog()
                streams = stdio_client(params, errlog=self.errlog.file)
            elif transport == "http":
                from mcp.client.streamable_http import streamable_http_client

                streams = streamable_http_client(cfg["url"])
            elif transport == "sse":
                from mcp.client.sse import sse_client

                streams = sse_client(cfg["url"], headers=dict(cfg.get("headers") or {}))
            else:
                raise RuntimeError(f"不支持的 transport：{transport}")

            async with AsyncExitStack() as stack:
                entered = await stack.enter_async_context(streams)
                read, write = entered[0], entered[1]
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                result = await session.list_tools()
                self.session = session
                self.tools = list(getattr(result, "tools", []) or [])
                # resources / prompts（失败不影响工具可用：有些 server 不实现）
                self.resources = await self._safe_list(session.list_resources)
                self.prompts = await self._safe_list(session.list_prompts)
                self.status = "ready"
                self.error = ""
                ready.set()
                await self.stop_event.wait()
        except Exception as e:                       # noqa: BLE001 - 要如实报告给用户
            self.status = "error"
            self.error = _describe_error(e)
            # 子进程自己的报错往往才是真正原因（命令不存在 / 镜像缺依赖 / 权限）
            tail = self.errlog.tail() if self.errlog else ""
            if tail:
                self.error = f"{self.error} —— 进程输出：{tail}"
            logger.warning("MCP server %r 启动失败：%s", self.name, self.error)
            ready.set()
        finally:
            self.session = None
            if self.status == "ready":
                self.status = "stopped"

    @staticmethod
    async def _safe_list(fn) -> list:
        """列 resources/prompts —— 不支持就当成空，不该让整个连接失败。"""
        try:
            res = await fn()
            return list(getattr(res, "resources", None) or getattr(res, "prompts", None) or [])
        except Exception:                            # noqa: BLE001
            return []


# ---------------------------------------------------------------- 连接管理


def connect(name: str, wait: bool = True) -> tuple[bool, str]:
    """连接一个 server。``wait=False`` 时只提交、不等结果（用于启动后台连接）。"""
    cfg = mcp_config.get_server(name)
    if cfg is None:
        return False, f"没有这个 server：{name}"

    with _state["lock"]:
        existing = _state["servers"].get(name)
        if existing and existing.status in ("ready", "connecting"):
            return True, existing.status

        loop = _ensure_loop()
        srv = _Server(cfg)
        srv.status = "connecting"
        _state["servers"][name] = srv
        ready = threading.Event()
        srv.future = asyncio.run_coroutine_threadsafe(srv._run(ready), loop)

    if not wait:
        _register_later(name, srv, ready)
        return True, "connecting"

    if not ready.wait(timeout=srv.timeout + 15):
        srv.status = "error"
        srv.error = f"连接超时（{srv.timeout + 15}s）"
        disconnect(name)
        return False, srv.error

    if srv.status != "ready":
        return False, srv.error or "连接失败"
    _register_tools(name, srv)
    return True, f"已连接，{len(srv.tools)} 个工具"


def _register_later(name: str, srv: _Server, ready: threading.Event):
    """后台等待连接完成，就绪后登记工具（不阻塞调用方）。"""

    def _wait():
        ready.wait(timeout=srv.timeout + 15)
        if srv.status == "ready":
            _register_tools(name, srv)
        elif srv.status == "connecting":
            srv.status = "error"
            srv.error = "连接超时"

    threading.Thread(target=_wait, name=f"mcp-ready-{name}", daemon=True).start()


def _register_tools(name: str, srv: _Server):
    """把 server 的能力登记进注册表。

    resources / prompts 不是 MCP 的「工具」，但对模型来说
    「列出来 / 按 uri 读」就是一个动作，所以合成成工具暴露。
    只有该 server 真的有 resource/prompt 时才合成 —— 免得白占 schema。
    """
    with _state["lock"]:
        for t in srv.tools:
            _state["registry"][make_full_name(name, t.name)] = {
                "server": name, "kind": "tool", "name": t.name,
            }
        if srv.resources:
            for fn in (RES_LIST, RES_READ):
                _state["registry"][make_full_name(name, fn)] = {
                    "server": name, "kind": "resource", "name": fn,
                }
        if srv.prompts:
            for fn in (PROMPT_LIST, PROMPT_GET):
                _state["registry"][make_full_name(name, fn)] = {
                    "server": name, "kind": "prompt", "name": fn,
                }


def disconnect(name: str, timeout: int = 12) -> bool:
    with _state["lock"]:
        srv = _state["servers"].pop(name, None)
    if srv is None:
        return False

    # 撤销该 server 的所有工具登记
    with _state["lock"]:
        for full, entry in list(_state["registry"].items()):
            if entry.get("server") == name:
                _state["registry"].pop(full, None)

    loop = _state["loop"]
    if loop is not None and srv.stop_event is not None:
        loop.call_soon_threadsafe(srv.stop_event.set)
    elif loop is not None and srv.future is not None:
        loop.call_soon_threadsafe(srv.future.cancel)

    if srv.future is not None:
        try:
            srv.future.result(timeout=timeout)
        except Exception:                      # noqa: BLE001 - 关闭失败不应影响主流程
            try:
                srv.future.cancel()
            except Exception:                  # noqa: BLE001
                pass
    if srv.errlog is not None:
        srv.errlog.close()
        srv.errlog = None
    srv.status = "stopped"
    return True


def stop_watchdog():
    _watchdog_stop.set()


def disconnect_all():
    for name in list(_state["servers"].keys()):
        try:
            disconnect(name)
        except Exception:                      # noqa: BLE001
            logger.warning("关闭 MCP server %r 失败", name, exc_info=True)


# ---------------------------------------------------------------- 看门狗（二期）
#
# MCP server 是独立进程，可能因为崩溃、被 OOM 杀掉、或自己退出而消失。
# 没有看门狗的话，界面上会一直显示「已连接」，而实际上每次调用都失败。

WATCHDOG_INTERVAL = 20      # 健康检查间隔（秒）
_MAX_BACKOFF = 120          # 重连退避上限（秒）
_watchdog_started = False
_watchdog_stop = threading.Event()
_watchdog_lock = threading.Lock()
_backoff = {}               # name -> (下次可重试时间, 当前退避秒数)


def _ping(srv: _Server, timeout: float = 6.0) -> bool:
    """给 server 发一次 MCP ping，判断进程还活着。"""
    loop = _state["loop"]
    if srv.session is None or loop is None:
        return False
    if _on_loop_thread():
        return True          # 看门狗不在 loop 线程；真到了这里只能放过，避免死锁
    try:
        fut = asyncio.run_coroutine_threadsafe(srv.session.send_ping(), loop)
        fut.result(timeout=timeout)
        return True
    except Exception:         # noqa: BLE001 - 任何异常都算不健康
        return False


def _try_reconnect(name: str) -> bool:
    logger.info("MCP %s 尝试重连…", name)
    old = _state["servers"].get(name)
    restarts = (old.restart_count + 1) if old else 1
    disconnect(name)
    ok, msg = connect(name, wait=True)
    srv = _state["servers"].get(name)
    if srv is not None:
        srv.restart_count = restarts
    if ok:
        _backoff.pop(name, None)
        logger.info("MCP %s 重连成功（第 %d 次）：%s", name, restarts, msg)
        return True
    cur = _backoff.get(name, (0.0, WATCHDOG_INTERVAL))[1]
    nxt = min(cur * 2, _MAX_BACKOFF)
    _backoff[name] = (time.time() + nxt, nxt)
    logger.warning("MCP %s 重连失败（%s），%.0fs 后再试", name, msg, nxt)
    return False


def _watchdog_tick():
    now = time.time()
    for name, srv in list(_state["servers"].items()):
        cfg = mcp_config.get_server(name)
        if not cfg or not cfg.get("enabled"):
            continue

        if srv.status == "ready":
            if not _ping(srv):
                logger.warning("MCP %s 健康检查失败（进程可能已退出）", name)
                srv.status = "error"
                srv.error = "健康检查失败：进程可能已退出"
                _backoff[name] = (now, WATCHDOG_INTERVAL)
            continue

        if srv.status in ("error", "stopped") and cfg.get("auto_reconnect", True):
            ready_at, _cur = _backoff.get(name, (now, WATCHDOG_INTERVAL))
            if now >= ready_at:
                _try_reconnect(name)


def _watchdog_loop():
    while not _watchdog_stop.wait(WATCHDOG_INTERVAL):
        try:
            _watchdog_tick()
        except Exception:                       # noqa: BLE001 - 看门狗自己不能挂
            logger.warning("MCP 看门狗本轮异常", exc_info=True)


def start_watchdog():
    global _watchdog_started
    with _watchdog_lock:
        if _watchdog_started:
            return
        _watchdog_started = True
    threading.Thread(target=_watchdog_loop, name="mcp-watchdog", daemon=True).start()
    logger.info("MCP 看门狗已启动（每 %ds 检查一次）", WATCHDOG_INTERVAL)


# ---------------------------------------------------------------- 工具调用


def _format_result(result) -> str:
    """把各种 MCP 结果转成给模型看的纯文本。"""
    # ---- ListResourcesResult / ListPromptsResult ----
    res_list = getattr(result, "resources", None)
    if res_list is not None:
        if not res_list:
            return "（没有可用资源）"
        return "\n".join(
            f"- {getattr(r, 'uri', '?')}  {getattr(r, 'name', '')}"
            f"{'  ' + r.description if getattr(r, 'description', None) else ''}"
            for r in res_list
        )
    prompt_list = getattr(result, "prompts", None)
    if prompt_list is not None:
        if not prompt_list:
            return "（没有可用提示词模板）"
        return "\n".join(
            f"- {getattr(pr, 'name', '?')}  {getattr(pr, 'description', '') or ''}"
            for pr in prompt_list
        )

    # ---- ReadResourceResult ----
    contents = getattr(result, "contents", None)
    if contents is not None:
        chunks = []
        for c in contents:
            text = getattr(c, "text", None)
            if text:
                chunks.append(text)
            else:
                chunks.append(f"[二进制资源 {getattr(c, 'uri', '?')} "
                              f"mime={getattr(c, 'mime_type', '?')}，已省略内容]")
        return "\n".join(chunks) or "（资源为空）"

    # ---- GetPromptResult ----
    messages = getattr(result, "messages", None)
    if messages is not None:
        chunks = []
        for m in messages:
            content = getattr(m, "content", None)
            text = getattr(content, "text", None)
            role = getattr(m, "role", "?")
            if text:
                chunks.append(f"[{role}] {text}")
        return "\n".join(chunks) or "（提示词为空）"

    parts = []
    for block in getattr(result, "content", None) or []:
        btype = getattr(block, "type", "")
        if btype == "text":
            parts.append(getattr(block, "text", "") or "")
        elif btype == "image":
            # **绝不把 base64 塞进上下文**：又贵又会把日志撑爆
            parts.append(f"[图片 mime={getattr(block, 'mimeType', '?')}，已省略内容]")
        elif btype == "resource":
            res = getattr(block, "resource", None)
            text = getattr(res, "text", None)
            parts.append(text if text else f"[资源 {getattr(res, 'uri', '?')}]")
        else:
            parts.append(f"[{btype or '未知'} 内容]")
    text = "\n".join(p for p in parts if p)
    if getattr(result, "isError", False):
        return f"❌ MCP 工具返回错误：{text or '（无内容）'}"
    return text or "（工具执行完成，无输出）"


async def _dispatch(srv: _Server, kind: str, name: str, args: dict, timeout: float):
    """按注册的 kind 分派到 session 上的对应方法。"""
    sess = srv.session
    if kind == "resource":
        if name == RES_LIST:
            return await sess.list_resources()
        return await sess.read_resource(args.get("uri", ""))
    if kind == "prompt":
        if name == PROMPT_LIST:
            return await sess.list_prompts()
        prompt_args = args.get("arguments")
        return await sess.get_prompt(
            name=args.get("name", ""),
            arguments=prompt_args if isinstance(prompt_args, dict) else None,
        )
    return await sess.call_tool(name, args, read_timeout_seconds=timeout)


def call_tool(full_name: str, args: dict, timeout: int | None = None) -> str:
    """同步调用一个 MCP 工具（供 ``tools.execute_tool`` 使用）。"""
    entry = resolve(full_name)
    if not entry:
        return f"❌ 未知的 MCP 工具：{full_name}（server 可能已断开）"
    server = entry["server"]
    kind = entry.get("kind", "tool")
    tool = entry["name"]

    srv = _state["servers"].get(server)
    if srv is None or srv.status != "ready" or srv.session is None:
        return f"❌ MCP server「{server}」当前不可用（状态：{srv.status if srv else '未连接'}）"

    if _on_loop_thread():
        # 在 loop 线程里再提交任务会死锁 —— 直接拒绝，避免整个进程卡死
        return "❌ 内部错误：不能在 MCP 事件循环线程里同步调用工具"

    loop = _state["loop"]
    t = int(timeout or srv.timeout)
    try:
        fut = asyncio.run_coroutine_threadsafe(
            _dispatch(srv, kind, tool, args or {}, float(t)), loop,
        )
    except Exception as e:                     # noqa: BLE001
        return f"❌ 调用 MCP 工具失败：{e}"

    try:
        result = fut.result(timeout=t + 10)
    except TimeoutError:
        fut.cancel()
        return f"❌ MCP 工具「{tool}」超时（{t}s）"
    except Exception as e:                     # noqa: BLE001
        return f"❌ MCP 工具「{tool}」执行出错：{e}"
    return _format_result(result)


# ---------------------------------------------------------------- 供工具系统用


def ready_servers() -> list[_Server]:
    return [s for s in _state["servers"].values() if s.status == "ready"]


def server_status(name: str) -> dict:
    srv = _state["servers"].get(name)
    if srv is None:
        return {"status": "idle", "error": "", "tool_count": 0,
                "resource_count": 0, "prompt_count": 0, "restart_count": 0}
    return {
        "status": srv.status, "error": srv.error, "tool_count": len(srv.tools),
        "resource_count": len(srv.resources), "prompt_count": len(srv.prompts),
        "restart_count": srv.restart_count,
    }


def tool_schemas() -> list[dict]:
    """把已就绪 server 的工具转成 OpenAI 风格 schema（**不阻塞**）。"""
    out = []
    for srv in ready_servers():
        for t in srv.tools:
            full = make_full_name(srv.name, t.name)
            if resolve(full) is None:
                continue
            ann = getattr(t, "annotations", None)
            notes = []
            if ann is not None:
                if getattr(ann, "read_only_hint", None):
                    notes.append("该工具自述为只读")
                if getattr(ann, "destructive_hint", None):
                    notes.append("该工具自述有破坏性")
                if getattr(ann, "open_world_hint", None):
                    notes.append("该工具自述会访问外部系统")
            desc = getattr(t, "description", "") or ""
            if notes:
                desc = f"{desc}\n（{ '；'.join(notes) }。注意：这是 server 自己的声明，未必可信）"
            out.append({
                "type": "function",
                "function": {
                    "name": full,
                    "description": f"[MCP:{srv.name}] {desc}".strip(),
                    "parameters": getattr(t, "input_schema", None) or {
                        "type": "object", "properties": {},
                    },
                },
            })

        # ---- resources / prompts 合成的工具 ----
        if srv.resources:
            out.append(_synth(
                make_full_name(srv.name, RES_LIST),
                f"[MCP:{srv.name}] 列出这个插件可读取的资源（文件/数据源）",
                {"type": "object", "properties": {}},
            ))
            out.append(_synth(
                make_full_name(srv.name, RES_READ),
                f"[MCP:{srv.name}] 按 uri 读取一个资源的内容",
                {"type": "object", "properties": {
                    "uri": {"type": "string", "description": "资源的 uri"}},
                    "required": ["uri"]},
            ))
        if srv.prompts:
            out.append(_synth(
                make_full_name(srv.name, PROMPT_LIST),
                f"[MCP:{srv.name}] 列出这个插件提供的提示词模板",
                {"type": "object", "properties": {}},
            ))
            out.append(_synth(
                make_full_name(srv.name, PROMPT_GET),
                f"[MCP:{srv.name}] 取一个提示词模板的内容",
                {"type": "object", "properties": {
                    "name": {"type": "string", "description": "模板名"},
                    "arguments": {"type": "object", "description": "模板参数"}},
                    "required": ["name"]},
            ))
    return out


def _synth(name: str, desc: str, params: dict) -> dict:
    return {"type": "function",
            "function": {"name": name, "description": desc, "parameters": params}}


def permission_kind(full_name: str) -> str:
    """MCP 工具的权限档位 —— 由用户对该 server 的标注决定，默认 mutate。"""
    entry = resolve(full_name)
    if not entry:
        return "mutate"
    # 列资源 / 读资源 / 取提示词都是只读动作，不跟 server 的写权限档位走
    if entry.get("kind") in ("resource", "prompt"):
        return "read"
    cfg = mcp_config.get_server(entry["server"]) or {}
    kind = cfg.get("permission") or "mutate"
    return kind if kind in mcp_config.PERMISSIONS else "mutate"


def start_background_connect() -> None:
    """启动时后台连接所有 enabled 的 server（**不阻塞启动**）。"""

    def _boot():
        try:
            for cfg in mcp_config.list_servers():
                if cfg["enabled"]:
                    ok, msg = connect(cfg["name"], wait=True)
                    logger.info("MCP %s：%s（%s）", cfg["name"], "已连接" if ok else "失败", msg)
        except Exception:                      # noqa: BLE001
            logger.warning("后台连接 MCP server 失败", exc_info=True)

    start_watchdog()
    threading.Thread(target=_boot, name="mcp-boot", daemon=True).start()


atexit.register(stop_watchdog)
atexit.register(disconnect_all)
