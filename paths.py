"""统一路径管理，兼容 Linux / Windows / PyInstaller。"""

import os
import sys


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def get_base_dir() -> str:
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def get_resource_dir() -> str:
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

DB_PATH = os.path.join(DATA_DIR, "chat.db")
CHROMA_DIR = os.path.join(DATA_DIR, "chroma_db")
TRASH_DIR = os.path.join(DATA_DIR, ".trash")
BACKUP_DIR = os.path.join(DATA_DIR, ".backup")
WEBVIEW_DATA_DIR = os.path.join(DATA_DIR, ".webview_data")
TEMPLATES_DIR = os.path.join(RESOURCE_DIR, "templates")
ICON_PATH = os.path.join(RESOURCE_DIR, "icon.png")


def get_user_home() -> str:
    return os.path.expanduser("~")


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
