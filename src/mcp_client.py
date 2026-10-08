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
import threading

import mcp_config

logger = logging.getLogger(__name__)

MCP_PREFIX = "mcp__"
_MAX_TOOL_NAME = 64          # OpenAI function name 上限

_state = {
    "loop": None,
    "thread": None,
    "servers": {},           # name -> _Server
    "registry": {},          # full_name -> (server_name, original_tool_name)
    "lock": threading.RLock(),
}


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
    """反查 (server, tool)。用注册表而不是解析字符串 —— server 名里允许下划线。"""
    return _state["registry"].get(full_name)


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


class _Server:
    """一个 MCP server 的运行时状态。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.name = cfg["name"]
        self.status = "idle"          # idle | connecting | ready | error | stopped
        self.error = ""
        self.tools = []               # list[mcp.types.Tool]
        self.session = None
        self.stop_event = None        # asyncio.Event（在 loop 线程里创建）
        self.future = None            # concurrent.futures.Future

    @property
    def timeout(self) -> int:
        return int(self.cfg.get("timeout") or 30)

    async def _run(self, ready: threading.Event):
        from contextlib import AsyncExitStack

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        self.stop_event = asyncio.Event()
        try:
            cfg = self.cfg
            if cfg["transport"] != "stdio":
                raise RuntimeError(f"暂不支持 {cfg['transport']}（本期只做 stdio）")

            params = StdioServerParameters(
                command=cfg["command"],
                args=list(cfg["args"]),
                env=mcp_config.server_env(cfg),
                cwd=cfg["cwd"] or None,
            )
            async with AsyncExitStack() as stack:
                read, write = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                result = await session.list_tools()
                self.session = session
                self.tools = list(getattr(result, "tools", []) or [])
                self.status = "ready"
                ready.set()
                await self.stop_event.wait()
        except Exception as e:                       # noqa: BLE001 - 要如实报告给用户
            self.status = "error"
            self.error = str(e)[:400]
            logger.warning("MCP server %r 启动失败：%s", self.name, self.error)
            ready.set()
        finally:
            self.session = None
            if self.status == "ready":
                self.status = "stopped"


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
    with _state["lock"]:
        for t in srv.tools:
            full = make_full_name(name, t.name)
            _state["registry"][full] = (name, t.name)


def disconnect(name: str, timeout: int = 12) -> bool:
    with _state["lock"]:
        srv = _state["servers"].pop(name, None)
    if srv is None:
        return False

    # 撤销该 server 的所有工具登记
    with _state["lock"]:
        for full, (sname, _t) in list(_state["registry"].items()):
            if sname == name:
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
    srv.status = "stopped"
    return True


def disconnect_all():
    for name in list(_state["servers"].keys()):
        try:
            disconnect(name)
        except Exception:                      # noqa: BLE001
            logger.warning("关闭 MCP server %r 失败", name, exc_info=True)


# ---------------------------------------------------------------- 工具调用


def _format_result(result) -> str:
    """把 CallToolResult 转成给模型看的纯文本。"""
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


def call_tool(full_name: str, args: dict, timeout: int | None = None) -> str:
    """同步调用一个 MCP 工具（供 ``tools.execute_tool`` 使用）。"""
    entry = resolve(full_name)
    if not entry:
        return f"❌ 未知的 MCP 工具：{full_name}（server 可能已断开）"
    server, tool = entry

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
            srv.session.call_tool(tool, args or {}, read_timeout_seconds=float(t)),
            loop,
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
        return {"status": "idle", "error": "", "tool_count": 0}
    return {"status": srv.status, "error": srv.error, "tool_count": len(srv.tools)}


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
    return out


def permission_kind(full_name: str) -> str:
    """MCP 工具的权限档位 —— 由用户对该 server 的标注决定，默认 mutate。"""
    entry = resolve(full_name)
    if not entry:
        return "mutate"
    cfg = mcp_config.get_server(entry[0]) or {}
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

    threading.Thread(target=_boot, name="mcp-boot", daemon=True).start()


atexit.register(disconnect_all)
