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


# ===== 项目根目录（按会话分桶，跨请求保留）=====
_project_roots = {}  # {session_key: path}


def set_project_root(path: str, session_id=None):
    _project_roots[session_key(session_id)] = path


def get_project_root(session_id=None):
    import os
    return _project_roots.get(session_key(session_id)) or os.path.expanduser("~")


# ===== Pending Session（中断续跑，按会话分桶）=====
# {session_key: {'messages': [...], 'step': N, 'task': '...', 'pending_tool': {...}}}
_pending_states = {}


def set_pending_state(state, session_id=None):
    """保存中断状态：{'messages': [...], 'step': N, 'task': '...'}"""
    _pending_states[session_key(session_id)] = state


def get_pending_state(session_id=None):
    return _pending_states.get(session_key(session_id))


def clear_pending_state(session_id=None):
    _pending_states.pop(session_key(session_id), None)


# ===== Pending Text（确认前已输出的文本，按会话分桶）=====
_pending_texts = {}  # {session_key: text}


def set_pending_text(text: str, session_id=None):
    _pending_texts[session_key(session_id)] = text


def get_pending_text(session_id=None) -> str:
    return _pending_texts.get(session_key(session_id), "")


def clear_pending_text(session_id=None):
    _pending_texts.pop(session_key(session_id), None)


# ===== 生命周期维护 =====

def clear_session(session_id):
    """清理某个会话的全部粘性状态（删除会话时调用，避免内存泄漏）。"""
    key = str(session_id)
    _project_roots.pop(key, None)
    _pending_states.pop(key, None)
    _pending_texts.pop(key, None)


def reset_all():
    """清空全部粘性状态（测试用）。"""
    _project_roots.clear()
    _pending_states.clear()
    _pending_texts.clear()
