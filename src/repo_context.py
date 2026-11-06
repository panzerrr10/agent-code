"""代码库索引（repo map）的上下文注入。

## 为什么需要它

改动前，Agent「理解项目」的唯一手段是 ``search_files`` + 逐个 ``read_file``
——**它没有项目全局观**。大项目里定位代码只能靠碰关键词 + 翻文件，
步数、token 和错误率会一起上去。Aider 的 repo map 就是为解决这个问题而存在的。

这里做的是同一件事的轻量版：把「哪些文件、有哪些符号、行号在哪」压成一段
紧凑索引，作为**独立的一条 system 消息**注入。

## 几个刻意的设计

- **独立成一条 system 消息**（与 ``project_context`` 一致）：
  主提示词保持稳定，对 prompt caching 友好；调试时也能一眼看出这段是索引。
- **按当前任务做 focus 加权**：同一个仓库，做「改登录」和「改导出」时
  该出现在索引顶部的文件不一样。所以每次运行重新构建一次 ——
  实测 65 个文件约 150ms、1200 个文件约 20ms（纯目录扫描 + 正则），
  相对一次 LLM 调用可以忽略，因此**不做缓存**（永不过期也就不会给模型过期信息）。
- **没有源码文件时不注入**：别往上下文里塞一段「（空）」。
- **大小可配**：``AGENT_REPO_MAP_CHARS``（默认 8000 字符 ≈ 2~3K tokens）。
  索引是要**每一步都重发**的，所以不能贪大。
- ``AGENT_REPO_MAP=0`` 可以整体关掉。
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

DEFAULT_MAX_CHARS = 8000
# 少于这么多源码文件时不必注入 —— 直接 list_dir 就够了，注入反而占上下文
MIN_FILES = 4

# 单个仓库最多索引多少个文件（排序后取前 N 个）
MAX_FILES = 400

# 「这看起来像一个项目吗」的标记文件。有任意一个就认为是项目根。
PROJECT_MARKERS = (
    ".git", ".hg", ".svn",
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
    "package.json", "tsconfig.json",
    "go.mod", "Cargo.toml", "pom.xml", "build.gradle", "build.gradle.kts",
    "settings.gradle", "CMakeLists.txt", "Makefile", "composer.json",
    "Gemfile", "mix.exs", "pubspec.yaml",
)

# 没有项目标记时，源码文件超过这个数就**不注入**。
#
# 为什么需要这条：**默认工作区是用户主目录**。实测对 $HOME 建索引会扫出
# 5973 个源码文件，索引里全是 `PycharmProjects/agent-code-archive/.../paths.py`
# 这种**无关项目**的路径和符号 —— 既白烧 token，也把用户其它项目的信息
# 送进了模型上下文。而「6000 个文件的目录」本来就不是一个仓库，
# 给它建 repo map 没有意义。
#
# 判定用有界预扫描（数到上限就停），所以对主目录这种大目录是**廉价**的。
MARKERLESS_MAX_FILES = 60

_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    "dist", "build", "target", ".idea", ".pytest_cache", ".ruff_cache", ".mypy_cache",
}


def _looks_like_project(root: str) -> bool:
    """根目录里有没有项目标记文件。"""
    for marker in PROJECT_MARKERS:
        if os.path.exists(os.path.join(root, marker)):
            return True
    # 通配类标记：*.csproj / *.sln / *.xcodeproj
    try:
        for name in os.listdir(root):
            low = name.lower()
            if low.endswith((".csproj", ".sln", ".xcodeproj", ".vcxproj")):
                return True
    except OSError:
        return False
    return False


def _count_source_files(root: str, limit: int) -> int:
    """有界预扫描：数 root 下有多少源码文件，数到 limit 就提前返回。"""
    try:
        import repomap
    except Exception:                                        # noqa: BLE001
        return 0

    count = 0
    try:
        for _dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in _SKIP_DIRS]
            for name in files:
                if os.path.splitext(name)[1].lower() in repomap.EXT_LANGUAGE:
                    count += 1
                    if count > limit:
                        return count
    except OSError:
        return count
    return count


def _enabled() -> bool:
    raw = os.getenv("AGENT_REPO_MAP")
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _max_chars() -> int:
    raw = os.getenv("AGENT_REPO_MAP_CHARS")
    try:
        value = int(raw) if raw else DEFAULT_MAX_CHARS
    except (TypeError, ValueError):
        return DEFAULT_MAX_CHARS
    return max(1000, min(value, 40000))


def build(project_root: str | None, focus: str = "") -> dict:
    """构建索引。返回 ``{"text", "files", "indexed", "symbols", "languages",
    "truncated", "enabled", "skipped"}``；没内容时 ``text`` 为空字符串。
    """
    empty = {
        "text": "",
        "files": 0,
        "indexed": 0,
        "symbols": 0,
        "languages": {},
        "truncated": False,
        "enabled": _enabled(),
        "skipped": "",
    }
    if not _enabled() or not project_root:
        return empty
    if not os.path.isdir(project_root):
        return empty

    # 「这是一个项目吗」的闸门 —— 见 MARKERLESS_MAX_FILES 的说明。
    # 默认工作区是用户主目录，不设这道闸门就会把整个 home 的源码索引
    # 塞进每一次请求（实测 5973 个文件，且包含无关项目的路径）。
    if not _looks_like_project(project_root):
        found = _count_source_files(project_root, MARKERLESS_MAX_FILES)
        if found > MARKERLESS_MAX_FILES:
            logger.info(
                "工作区 %s 没有项目标记且有 %d+ 个源码文件，判定为「目录而非项目」，跳过代码库索引",
                project_root, found,
            )
            return {**empty, "files": found, "skipped": "not_a_project"}

    try:
        import repomap

        data = repomap.build_repo_map(
            project_root, max_chars=_max_chars(), max_files=MAX_FILES, focus=focus
        )
    except Exception:                                        # noqa: BLE001
        logger.warning("构建代码库索引失败（不影响主流程）", exc_info=True)
        return empty

    if data.get("files", 0) < MIN_FILES:
        # 小项目不值得占上下文
        return {**empty, "files": data.get("files", 0), "enabled": True}

    return {**data, "enabled": True, "skipped": ""}


def build_system_message(project_root: str | None, focus: str = "") -> tuple[dict | None, dict]:
    """返回 ``(system 消息或 None, 统计信息)``。"""
    data = build(project_root, focus=focus)
    if not data["text"]:
        return None, data

    header = (
        "# 代码库索引（自动生成）\n"
        f"下面是从工作区扫描出的代码结构索引（共 {data['files']} 个源码文件，"
        f"按「符号数 + 被引用次数」排序，只列出最相关的一部分）。\n"
        "**用途**：先从这里判断「该看哪个文件」，再用 read_file / get_function_body "
        "精读。不要凭文件名猜内容。\n"
    )
    if data.get("truncated"):
        header += "（索引已截断，只覆盖了部分文件 —— 找不到时请用 search_files 全仓搜索。）\n"

    return (
        {"role": "system", "content": header + "\n```\n" + data["text"] + "\n```"},
        data,
    )
