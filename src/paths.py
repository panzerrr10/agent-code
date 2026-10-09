"""统一路径管理，兼容 Linux / Windows / PyInstaller。

目录约定：**代码在 `<root>/src/`，前端资源在 `<root>/web/`，运行数据在 `<root>/data/`**。
因此 `get_base_dir()` 取的是 `src/` 的上一级（项目根），而不是 `paths.py` 自己所在的目录——
否则一旦调整代码目录，已有的会话 / 知识库 / 访问令牌就会"凭空消失"。
"""

import os
import sys

# 本文件所在目录（开发态 = <root>/src）
_SRC_DIR = os.path.dirname(os.path.abspath(__file__))


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def get_base_dir() -> str:
    """项目根目录（开发态下是 src/ 的上一级）。"""
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(_SRC_DIR)


def get_resource_dir() -> str:
    """打包后的资源解包目录；开发态下等同于项目根。"""
    if is_frozen() and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return get_base_dir()


def get_data_dir() -> str:
    base = get_base_dir()
    data = os.path.join(base, "data")
    try:
        os.makedirs(data, exist_ok=True)
        test = os.path.join(data, ".write_test")
        with open(test, "w") as f:
            f.write("ok")
        os.remove(test)
        return data
    except Exception:
        data = os.path.join(os.path.expanduser("~"), ".ai_agent_data")
        os.makedirs(data, exist_ok=True)
        return data


BASE_DIR = get_base_dir()
RESOURCE_DIR = get_resource_dir()
DATA_DIR = get_data_dir()

# 允许用环境变量把数据库指到别处（测试用独立临时库，避免污染真实数据）
DB_PATH = os.environ.get("AGENT_DB_PATH") or os.path.join(DATA_DIR, "chat.db")
CHROMA_DIR = os.path.join(DATA_DIR, "chroma_db")
# 回收站。支持环境变量覆盖 —— **测试与评估必须隔离它**：
# 光隔离 AGENT_DB_PATH 是不够的，跑一批任务会往真实回收站里塞几百份备份
# （agent-code 自己就踩过这个坑，往 data/.trash 留了 122 条测试残留）。
TRASH_DIR = os.environ.get("AGENT_TRASH_DIR") or os.path.join(DATA_DIR, ".trash")
BACKUP_DIR = os.path.join(DATA_DIR, ".backup")
WEBVIEW_DATA_DIR = os.path.join(DATA_DIR, ".webview_data")

# 前端资源：<root>/web/{templates,static,icon.png}
WEB_DIR = os.path.join(RESOURCE_DIR, "web")
TEMPLATES_DIR = os.path.join(WEB_DIR, "templates")
STATIC_DIR = os.path.join(WEB_DIR, "static")
ICON_PATH = os.path.join(WEB_DIR, "icon.png")


def get_user_home() -> str:
    return os.path.expanduser("~")


# 附件上传目录。
# **刻意放在 home 下而不是 DATA_DIR**：Agent 工具的白名单（is_allowed_path）
# 只放行用户主目录；若把附件放在别处（项目不在 home 下时就是这种情况），
# Agent 的 read_file / edit_file 会读不到它，「让它改这本文档」就直接失效。
UPLOAD_DIR = os.path.join(get_user_home(), ".agent-code", "uploads")


def get_default_workspace() -> str:
    # 工作区 = 用户 home（与 System Prompt 一致）
    return get_user_home()


def get_forbidden_dirs():
    home = get_user_home()
    return [
        os.path.join(home, ".ssh"),
        os.path.join(home, ".gnupg"),
        os.path.join(home, ".config"),
        os.path.join(home, ".cache"),
        os.path.join(home, ".local"),
        os.path.join(home, ".aws"),
        os.path.join(home, ".docker"),
        os.path.join(home, ".kube"),
        os.path.join(home, "AppData", "Roaming", "Microsoft", "Credentials"),
        os.path.join(home, "AppData", "Local", "Microsoft", "Credentials"),
    ]


def get_allowed_dirs():
    return [get_user_home()]


def is_allowed_path(path: str) -> bool:
    try:
        real = os.path.realpath(path)
    except Exception:
        return False
    for forbidden in get_forbidden_dirs():
        try:
            fb = os.path.realpath(forbidden)
        except Exception:
            continue
        if real == fb or real.startswith(fb + os.sep):
            return False
    for allowed in get_allowed_dirs():
        try:
            al = os.path.realpath(allowed)
        except Exception:
            continue
        if real == al or real.startswith(al + os.sep):
            return True
    return False


def is_path_permitted(path: str) -> bool:
    """Web 接口专用路径校验：敏感目录一律拒绝，其余放行 home 或当前项目根目录。

    与 ``is_allowed_path`` 的区别：额外放行用户显式选择的 ``project_root``
    （可能位于 home 之外，例如 /tmp/proj），但仍然禁止 .ssh / .aws / .kube
    等敏感目录，避免文件预览、目录树接口绕过白名单。
    """
    try:
        real = os.path.realpath(path)
    except Exception:
        return False

    # 敏感目录优先拒绝（即使它恰好在 project_root 之内）
    for forbidden in get_forbidden_dirs():
        try:
            fb = os.path.realpath(forbidden)
        except Exception:
            continue
        if real == fb or real.startswith(fb + os.sep):
            return False

    candidates = list(get_allowed_dirs())
    try:
        import runtime

        root = runtime.get_project_root()
        if root:
            candidates.append(root)
    except Exception:
        pass

    for allowed in candidates:
        try:
            al = os.path.realpath(allowed)
        except Exception:
            continue
        if al and (real == al or real.startswith(al + os.sep)):
            return True
    return False
