"""内置 MCP 插件目录：常见 server 的一键添加模板。

**这里的包名都是核对过的**（npm / PyPI 实际存在），不是凭记忆写的 ——
目录里放一个不存在的包，用户点一下只会得到一个费解的错误。

分两类运行时：

- ``node``：``npx -y <包名>``，需要机器上有 Node
- ``uvx``：``uvx <包名>``，需要机器上有 uv（Python 的 uvx 工具）

两者都可能需要联网首次拉包，所以默认都要用户自己勾「允许联网」。
"""

from __future__ import annotations

import shutil

# runtime -> (可执行文件, 说明)
RUNTIMES = {
    "node": ("npx", "需要 Node.js（本机多数已装）"),
    "uvx": ("uvx", "需要 uv（Python 包运行器，本机可能未装）"),
}

CATALOG = [
    {
        "id": "filesystem",
        "name": "文件系统",
        "icon": "📁",
        "runtime": "node",
        "description": "读写指定目录下的文件：读取、写入、编辑、搜索、目录树。",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "{path}"],
        "placeholders": [{"key": "path", "label": "允许访问的目录", "default": "~/下载"}],
        "permission": "mutate",
        "keywords": ["文件", "目录", "filesystem", "读取文件"],
        "needs_network": True,
        "recommend_sandbox": True,
        "mount_from": ["path"],
        "notes": "官方 server。建议开沙箱并挂载该目录，这样它只能看到这一个目录。",
    },
    {
        "id": "memory",
        "name": "记忆图谱",
        "icon": "🧠",
        "runtime": "node",
        "description": "基于知识图谱的长期记忆：存实体、关系，并按名字检索。",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-memory"],
        "placeholders": [],
        "permission": "mutate",
        "keywords": ["记忆", "知识图谱", "memory", "记住"],
        "needs_network": True,
        "recommend_sandbox": False,
        "notes": "和内置的 save_memory 不同，这个是图结构、适合存实体关系。",
    },
    {
        "id": "sequential-thinking",
        "name": "顺序思考",
        "icon": "🪜",
        "runtime": "node",
        "description": "把复杂问题拆成可修订的思考步骤，支持回退与分支。",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
        "placeholders": [],
        "permission": "read",
        "keywords": ["思考", "推理", "拆解", "thinking"],
        "needs_network": True,
        "recommend_sandbox": False,
        "notes": "纯推理工具，不碰文件和命令，所以默认按只读处理。",
    },
    {
        "id": "everything",
        "name": "测试用全功能",
        "icon": "🧪",
        "runtime": "node",
        "description": "官方调试 server：包含工具、资源、提示词等各种能力，用来验证插件链路。",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-everything"],
        "placeholders": [],
        "permission": "mutate",
        "keywords": ["测试", "调试", "everything"],
        "needs_network": True,
        "recommend_sandbox": False,
        "notes": "适合第一次装插件时验证「连通、工具列表、资源、提示词」是否都正常。",
    },
    {
        "id": "fetch",
        "name": "网页抓取",
        "icon": "🌐",
        "runtime": "uvx",
        "description": "抓取网页并转成 Markdown，可供模型阅读。",
        "command": "uvx",
        "args": ["mcp-server-fetch"],
        "placeholders": [],
        "permission": "read",
        "keywords": ["网页", "抓取", "fetch", "url", "链接"],
        "needs_network": True,
        "recommend_sandbox": False,
        "notes": "⚠️ 这是**外发**能力：会把网址和内容发到外部。注意别把本机私密内容一起带上。",
    },
    {
        "id": "git",
        "name": "Git 仓库",
        "icon": "🌿",
        "runtime": "uvx",
        "description": "查看仓库状态、提交历史、差异、分支等（只读为主）。",
        "command": "uvx",
        "args": ["mcp-server-git", "--repository", "{path}"],
        "placeholders": [{"key": "path", "label": "Git 仓库路径", "default": "~"}],
        "permission": "read",
        "keywords": ["git", "仓库", "提交", "分支", "diff"],
        "needs_network": True,
        "recommend_sandbox": True,
        "mount_from": ["path"],
        "notes": "项目里已有 git_* 内置工具；装这个主要用于更细的历史查询。",
    },
    {
        "id": "time",
        "name": "时间时区",
        "icon": "🕐",
        "runtime": "uvx",
        "description": "获取当前时间、做时区换算。",
        "command": "uvx",
        "args": ["mcp-server-time"],
        "placeholders": [],
        "permission": "read",
        "keywords": ["时间", "时区", "现在几点"],
        "needs_network": True,
        "recommend_sandbox": False,
        "notes": "轻量只读工具。",
    },
    {
        "id": "sqlite",
        "name": "SQLite 查询",
        "icon": "🗄️",
        "runtime": "uvx",
        "description": "对 SQLite 数据库执行查询、查看表结构。",
        "command": "uvx",
        "args": ["mcp-server-sqlite", "--db-path", "{path}"],
        "placeholders": [{"key": "path", "label": "数据库文件", "default": "~/data.db"}],
        "permission": "mutate",
        "keywords": ["数据库", "sqlite", "sql", "查询"],
        "needs_network": True,
        "recommend_sandbox": True,
        "mount_from": ["path"],
        "notes": "可以写库，所以默认按「需要确认」处理。",
    },
]


def runtime_available(runtime: str) -> tuple[bool, str]:
    """检查某个运行时是否装了。"""
    info = RUNTIMES.get(runtime)
    if not info:
        return False, "未知运行时"
    exe = info[0]
    return (shutil.which(exe) is not None), info[1]


def list_catalog() -> list:
    """返回目录，并标注每项的运行时是否可用（不可用的置灰而不是隐藏）。"""
    out = []
    for item in CATALOG:
        ok, hint = runtime_available(item["runtime"])
        entry = dict(item)
        entry["runtime_available"] = ok
        entry["runtime_hint"] = hint
        out.append(entry)
    return out


def get_template(item_id: str) -> dict | None:
    for item in CATALOG:
        if item["id"] == item_id:
            return dict(item)
    return None


def build_config(item_id: str, values: dict, name: str | None = None) -> tuple[dict | None, str | None]:
    """把目录模板 + 用户填的占位符，变成一份插件配置。"""
    import os as _os

    tpl = get_template(item_id)
    if tpl is None:
        return None, f"目录里没有这一项：{item_id}"

    vals = {}
    for ph in tpl["placeholders"]:
        raw = (values or {}).get(ph["key"]) or ph.get("default") or ""
        raw = _os.path.expanduser(str(raw).strip())
        if not raw:
            return None, f"请填写「{ph['label']}」"
        vals[ph["key"]] = raw

    def _sub(s: str) -> str:
        for k, v in vals.items():
            s = s.replace("{" + k + "}", v)
        return s

    cfg = {
        "transport": "stdio",
        "command": tpl["command"],
        "args": [_sub(a) for a in tpl["args"]],
        "permission": tpl.get("permission", "mutate"),
        "inject": "auto",
        "keywords": list(tpl.get("keywords") or []),
        "allow_network": bool(tpl.get("needs_network")),
        "enabled": False,          # 目录一键添加也只到「配好但没启用」
        "sandbox": bool(tpl.get("recommend_sandbox")),
        "sandbox_image": "agent-mcp-sandbox",
        "sandbox_mounts": [vals[k] for k in (tpl.get("mount_from") or []) if k in vals],
    }
    return cfg, None
