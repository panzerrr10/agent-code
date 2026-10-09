"""项目约定文件（AGENTS.md）的读取。

让用户能给项目注入上下文 —— 把「通用助手」变成「懂这个项目的助手」。

只认 ``AGENTS.md``（用户明确要求不兼容 CLAUDE.md 等其他名字），查找顺序：

1. ``<工作区>/AGENTS.md``           —— 主推，放项目根目录
2. ``<工作区>/.agentcode/AGENTS.md`` —— 不想污染根目录时
3. ``~/.agentcode/AGENTS.md``        —— 用户级全局偏好

找到多个时**按顺序合并**（项目级在前，用户级在后），而不是只取第一个：
项目约定应当覆盖个人习惯。
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# 单个文件上限。防着有人写个 1MB 的说明把上下文吃光。
MAX_BYTES_PER_FILE = 8192
# 合并后的总上限（项目级 + 用户级）
MAX_BYTES_TOTAL = 16384

FILENAME = "AGENTS.md"


def _candidate_paths(project_root: str | None) -> list[str]:
    """按优先级列出候选路径（只返回真实存在的文件）。"""
    out = []
    if project_root:
        root = os.path.abspath(project_root)
        out.append(os.path.join(root, FILENAME))
        out.append(os.path.join(root, ".agentcode", FILENAME))
    home = os.path.expanduser("~")
    out.append(os.path.join(home, ".agentcode", FILENAME))
    return [p for p in out if os.path.isfile(p)]


def _read_capped(path: str, limit: int) -> tuple[str, bool]:
    """读文件并在超出上限时截断。返回 (内容, 是否被截断)。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            raw = f.read(limit + 1)
    except OSError:
        logger.warning("读取 %s 失败", path, exc_info=True)
        return "", False
    if len(raw) > limit:
        return raw[:limit], True
    return raw, False


def load(project_root: str | None) -> dict:
    """读取项目约定。

    返回 ``{"sources": [{"path","bytes","truncated"}], "text": str, "truncated": bool}``。
    没有找到任何文件时 ``text`` 为空字符串。
    """
    paths = _candidate_paths(project_root)
    if not paths:
        return {"sources": [], "text": "", "truncated": False}

    parts = []
    sources = []
    remaining = MAX_BYTES_TOTAL
    any_truncated = False

    for path in paths:
        if remaining <= 0:
            break
        content, cut = _read_capped(path, min(MAX_BYTES_PER_FILE, remaining))
        if not content.strip():
            continue
        remaining -= len(content)
        any_truncated = any_truncated or cut
        sources.append({
            "path": path,
            "bytes": os.path.getsize(path) if os.path.exists(path) else len(content),
            "chars": len(content),
            "truncated": cut,
        })
        title = "项目约定" if path.startswith(os.path.abspath(project_root or "/")) else "用户级约定"
        note = "（已截断）" if cut else ""
        parts.append(f"## {title}（来自 {path}）{note}\n{content.strip()}")

    if not parts:
        return {"sources": [], "text": "", "truncated": False}

    body = "\n\n".join(parts)
    if any_truncated:
        body += "\n\n（注：以上内容因超出大小上限已被截断。）"
    return {"sources": sources, "text": body, "truncated": any_truncated}


def target_path(project_root: str | None) -> str:
    """新建/编辑时用哪个路径 —— 默认工作区根目录下的 AGENTS.md。"""
    if project_root:
        return os.path.join(os.path.abspath(project_root), FILENAME)
    return os.path.join(os.path.expanduser("~"), ".agentcode", FILENAME)


def build_system_message(project_root: str | None) -> dict | None:
    """构造注入用的 system 消息；没有 AGENTS.md 时返回 None。

    **刻意做成独立的一条 system 消息**，而不是拼进 SYSTEM_PROMPT：
    主提示词保持稳定（对 prompt caching 友好），调试时也能一眼看出哪段是用户写的。
    """
    data = load(project_root)
    if not data["text"]:
        return None
    return {
        "role": "system",
        "content": (
            "# 本项目的约定\n"
            "以下是用户为这个项目写下的约定与背景，请严格遵守：\n\n"
            + data["text"]
        ),
    }
