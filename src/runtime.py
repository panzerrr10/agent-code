"""运行时状态：每请求的 API 配置 + 按会话隔离的粘性状态。

设计说明：
- **API 配置**（api_key / base_url / rag_api_key / rag_base_url）是「每请求」的，
  用 ``contextvars.ContextVar`` 存储，天然按线程/执行上下文隔离，
  解决多并发请求互相覆盖 key 的竞态问题。
- **粘性状态**（project_root / pending_state / pending_text）需要「跨请求」保留
  （例如中断续跑：一个请求保存 pending_state，用户确认后由下一个请求读出），
  因此不能只用 ContextVar。
- 但粘性状态也不能是**单个全局变量**，否则并发会话会互相覆盖
  （A 会话的确认挂起被 B 会话冲掉、两个会话抢同一份计划）。
  因此这里改成 ``{session_key: value}`` 分桶存储：
  * 当前会话 id 由 ``set_session_id()`` 写入 ContextVar，
    由 agent/app/cli 在任务入口设置；
  * 工具层（被 LLM 间接调用、拿不到 session_id 参数）通过 ``session_key()``
    读取当前上下文，落到正确的桶里；
  * 拿不到会话时回落到 ``__default__`` 桶，保证 CLI 单次任务、脚本调用仍可用。

对外函数签名保持兼容（新增的 ``session_id`` 均为可选参数），
调用方（agent.py / rag.py / tools.py / app.py / cli.py）无需大改。
"""

import contextvars
import logging
import secrets
import threading

from state_store import SessionStateStore

logger = logging.getLogger(__name__)

DEFAULT_SESSION_KEY = "__default__"

# ===== 当前会话（每次执行上下文）=====
_session_id_var = contextvars.ContextVar("session_id", default=None)


def set_session_id(session_id):
    """设置当前执行上下文的会话 id（agent/app/cli 在任务入口调用）。"""
    _session_id_var.set(session_id)


def get_session_id():
    return _session_id_var.get()


def session_key(session_id=None) -> str:
    """解析粘性状态的分桶键。

    优先级：显式传入的 session_id > 当前上下文 > 默认桶。
    工具层拿不到 session_id 时依赖上下文，因此这里必须容错。
    """
    if session_id is not None:
        return str(session_id)
    sid = _session_id_var.get()
    return str(sid) if sid is not None else DEFAULT_SESSION_KEY


# ===== 主配置（每请求隔离）=====
_api_key_var = contextvars.ContextVar("api_key", default=None)
_base_url_var = contextvars.ContextVar("base_url", default=None)


def set_api_key(key: str):
    _api_key_var.set(key)


def get_api_key():
    return _api_key_var.get()


def set_base_url(url: str):
    _base_url_var.set(url)


def get_base_url():
    return _base_url_var.get()


# ===== RAG 专用配置（每请求隔离）=====
_rag_api_key_var = contextvars.ContextVar("rag_api_key", default=None)
_rag_base_url_var = contextvars.ContextVar("rag_base_url", default=None)


def set_rag_api_key(key: str):
    _rag_api_key_var.set(key)


def get_rag_api_key():
    return _rag_api_key_var.get()


def set_rag_base_url(url: str):
    _rag_base_url_var.set(url)


def get_rag_base_url():
    return _rag_base_url_var.get()


def get_effective_rag_api_key():
    """RAG 专用 key 优先，其次 .env 的 RAG_API_KEY，最后返回空（由 rag.get_client 从 config_providers 兜底）。"""
    import os
    return _rag_api_key_var.get() or os.getenv("RAG_API_KEY", "")


def get_effective_rag_base_url():
    """RAG 专用 base_url 优先，其次 .env 的 RAG_BASE_URL，最后返回空（由 rag.get_client 从 config_providers 兜底）。"""
    import os
    return _rag_base_url_var.get() or os.getenv("RAG_BASE_URL", "")


# ===== 工作目录（工作区说了算 + 可按会话临时覆盖）=====
# 优先级：本会话显式设置 > 会话所属工作区的 path > 用户主目录
_project_root_store = SessionStateStore("project_root")


def set_project_root(path: str, session_id=None):
    _project_root_store.set(session_key(session_id), path)


def clear_project_root(session_id=None):
    """清掉按会话的显式覆盖，让「会话所属工作区」的路径重新生效。

    前端不再自己存项目路径（那是双真相源的老毛病），所以每条 Web 请求都要先清一次
    历史遗留的覆盖值，否则老会话里存过的路径会一直盖住工作区设置。
    """
    _project_root_store.pop(session_key(session_id))


def get_project_root(session_id=None):
    """解析当前工作目录。

    **工作区是唯一真相源**：以前前端把路径存在全局 localStorage 里，后端按会话存，
    切换会话时前端那份全局值会把别的会话的路径覆盖掉。现在路径由「会话所属工作区」
    决定，显式设置只作为临时覆盖。
    """
    import os

    key = session_key(session_id)
    path = _project_root_store.get(key)
    if path:
        return path
    try:
        import db

        if str(key).isdigit():
            ws = db.get_workspace_of_session(int(key))
            if ws and ws.get("path"):
                return ws["path"]
    except Exception:
        logger.warning("解析会话所属工作区失败，回退到主目录", exc_info=True)
    return os.path.expanduser("~")


# ===== Pending Session（中断续跑，按会话分桶 + 落库）=====
# {'messages': [...], 'step': N, 'task': '...', 'pending_tool': {...}, 'confirm_token': ...}
#
# 带 24 小时 TTL：服务重启后仍能恢复「刚刚挂起的确认」，
# 但不会翻出一周前的旧确认让用户误点。
_pending_store = SessionStateStore("pending", ttl_seconds=24 * 3600)


def set_pending_state(state, session_id=None):
    """保存中断状态：{'messages': [...], 'step': N, 'task': '...'}"""
    _pending_store.set(session_key(session_id), state)


def get_pending_state(session_id=None):
    return _pending_store.get(session_key(session_id))


def clear_pending_state(session_id=None):
    _pending_store.pop(session_key(session_id))


def verify_and_consume_confirm_token(session_id, token) -> bool:
    """校验并**消费**一次性确认令牌（防重放）。

    令牌由 agent 在「需要人工确认」时生成并写进 pending_state。
    只有把它原样带回来，才允许执行那个挂起的危险操作——
    这样就杜绝了「只发一个 resume=true 就能跳过确认」的绕过。
    """
    if not token:
        return False
    state = get_pending_state(session_id)
    if not state:
        return False
    expected = state.get("confirm_token")
    if not expected or not secrets.compare_digest(str(expected), str(token)):
        return False
    state.pop("confirm_token", None)  # 一次性：校验通过立刻作废
    set_pending_state(state, session_id)
    return True


# ===== Pending Text（确认前已输出的文本，按会话分桶 + 落库）=====
_pending_text_store = SessionStateStore("pending_text")


def set_pending_text(text: str, session_id=None):
    _pending_text_store.set(session_key(session_id), text)


def get_pending_text(session_id=None) -> str:
    return _pending_text_store.get(session_key(session_id), "")


def clear_pending_text(session_id=None):
    _pending_text_store.pop(session_key(session_id))


# ===== 运行中断（按会话）=====
# {session_key: threading.Event}
# 每次运行都注册一个**全新**的 Event，因此上一次运行残留的「已取消」状态
# 不会误伤下一次运行（这是最容易踩的坑：复用同一个 Event 会导致新任务刚
# 开始就立刻自己停掉）。
_cancel_events = {}
_cancel_lock = threading.Lock()


def register_run(session_id=None):
    """开始一次运行：注册全新的取消标志并返回它。"""
    event = threading.Event()
    with _cancel_lock:
        _cancel_events[session_key(session_id)] = event
    return event


def request_cancel(session_id=None) -> bool:
    """请求中断某会话正在运行的任务。

    返回是否命中了「正在运行」的会话；协作式取消，真正的停止发生在
    Agent 主循环的下一个检查点。
    """
    with _cancel_lock:
        event = _cancel_events.get(session_key(session_id))
    if event is None:
        return False
    event.set()
    return True


def is_cancelled(session_id=None) -> bool:
    """当前会话是否已被请求中断。"""
    event = _cancel_events.get(session_key(session_id))
    return bool(event is not None and event.is_set())


def clear_run(session_id=None):
    """运行结束（正常 / 异常 / 客户端断开）时注销取消标志。"""
    with _cancel_lock:
        _cancel_events.pop(session_key(session_id), None)


# ===== 会话权限等级（1 只读 / 2 确认 / 3 完全）=====
def get_permission_level(session_id=None) -> int:
    """当前会话的权限等级。

    **服务端权威**：等级存在 sessions 表里、按会话读，前端传什么都不作数。
    """
    from config import DEFAULT_PERMISSION_LEVEL

    try:
        import db

        key = session_key(session_id)
        if str(key).isdigit():
            return db.get_permission_level(int(key))
    except Exception:
        logger.warning("读取会话权限等级失败，回退默认值", exc_info=True)
    return DEFAULT_PERMISSION_LEVEL


def get_cancel_event(session_id=None):
    """取当前会话的取消标志（没有在运行的任务时返回 None）。

    工具层用它做「工具级取消」：把 event 传进子进程 / 沙箱执行循环，
    用户点停止后立刻终止正在跑的命令，而不是等它自然结束
    （run_tests 最长 120 秒，等待体验很差）。
    """
    return _cancel_events.get(session_key(session_id))


# ===== 生命周期维护 =====

def clear_session(session_id):
    """清理某个会话的全部粘性状态（删除会话时调用，内存与库都清）。"""
    key = str(session_id)
    _project_root_store.pop(key)
    _pending_store.pop(key)
    _pending_text_store.pop(key)
    with _cancel_lock:
        _cancel_events.pop(key, None)
    try:  # 计划由 tools 持有，避免循环导入所以延迟引用
        import tools

        tools.clear_plan(session_id)
    except Exception:
        pass


def reset_all():
    """清空全部粘性状态（测试用：内存与持久化一起清，避免旧值复活）。"""
    _project_root_store.purge_all()
    _pending_store.purge_all()
    _pending_text_store.purge_all()
    with _cancel_lock:
        _cancel_events.clear()
    try:
        import tools

        tools.reset_plans()
    except Exception:
        pass
