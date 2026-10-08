"""MCP 插件配置：``data/mcp.json`` 的读写与校验。

设计原则（安全默认）：

- **新加的 server 默认 ``enabled=False``** —— 装上不等于启用，必须用户手动打开。
  第三方 server 是任意代码，默认放行等于默认信任。
- **默认 ``allow_network=False``** —— 联网必须逐 server 显式授权。
- **默认 ``permission="mutate"``** —— 宁可多要一次确认，也不默认只读。
- server 名会被拼进工具名（``mcp__<server>__<tool>``），所以只允许安全字符。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil

from paths import DATA_DIR

logger = logging.getLogger(__name__)

# 测试用 AGENT_MCP_PATH 指到临时目录，避免污染真实配置
MCP_FILE = os.environ.get("AGENT_MCP_PATH") or os.path.join(DATA_DIR, "mcp.json")

# server 名会出现在工具名里，必须严格限制（也顺带防注入）
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")

TRANSPORTS = ("stdio", "http", "sse")
INJECT_MODES = ("auto", "always", "never")
PERMISSIONS = ("read", "internal", "mutate", "execute")

# 传给 MCP server 的环境变量白名单。
# **绝不整个 os.environ 传过去** —— 那会把 .env 里的 API Key、
# 各种 token 直接交给一个第三方进程。只给它能跑起来的最小集合。
ENV_WHITELIST = (
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ",
    "NODE_PATH", "NVM_DIR", "PYTHONPATH", "VIRTUAL_ENV",
)

DEFAULT_SERVER = {
    "transport": "stdio",
    "command": "",
    "args": [],
    "url": "",
    "headers": {},           # http/sse 用的请求头（可能含 token，接口只回键名）
    "env": {},
    "cwd": "",
    "enabled": False,        # 装上 ≠ 启用
    "allow_network": False,  # 联网必须显式授权
    "permission": "mutate",  # 默认最保守
    "inject": "auto",        # 按需注入
    "keywords": [],
    "timeout": 30,
    "auto_reconnect": True,   # 进程挂掉后自动拉起（看门狗）
    # ---- 沙箱（二期）----
    # 默认 False：宿主执行。改成 True 会把 server 关进 Docker，
    # **但注意默认镜像里没有 node**，npx 类插件跑不起来，需要 node 镜像
    # （见 scripts/mcp-sandbox.Dockerfile）。python/uvx 类插件可直接沙箱。
    "sandbox": False,
    "sandbox_image": "agent-sandbox",
    "sandbox_mounts": [],    # 要挂进容器的宿主目录（读写，挂到同路径）
}


def _empty() -> dict:
    return {"servers": {}, "inject_enabled": True}


def load_config() -> dict:
    try:
        with open(MCP_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return _empty()
    except Exception:
        logger.warning("读取 %s 失败，按空配置处理", MCP_FILE, exc_info=True)
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    data.setdefault("servers", {})
    data.setdefault("inject_enabled", True)
    if not isinstance(data["servers"], dict):
        data["servers"] = {}
    return data


def save_config(cfg: dict) -> None:
    os.makedirs(os.path.dirname(MCP_FILE) or ".", exist_ok=True)
    tmp = MCP_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MCP_FILE)


def normalize(name: str, raw: dict) -> dict:
    """把一条 server 配置补齐成完整结构（缺失项用安全默认值）。"""
    cfg = dict(DEFAULT_SERVER)
    cfg.update({k: v for k, v in (raw or {}).items() if k in DEFAULT_SERVER})
    cfg["name"] = name
    if cfg["transport"] not in TRANSPORTS:
        cfg["transport"] = "stdio"
    if cfg["permission"] not in PERMISSIONS:
        cfg["permission"] = "mutate"
    if cfg["inject"] not in INJECT_MODES:
        cfg["inject"] = "auto"
    cfg["enabled"] = bool(cfg["enabled"])
    cfg["allow_network"] = bool(cfg["allow_network"])
    cfg["auto_reconnect"] = bool(cfg["auto_reconnect"])
    if not isinstance(cfg["args"], list):
        cfg["args"] = []
    cfg["args"] = [str(a) for a in cfg["args"]]
    if not isinstance(cfg["env"], dict):
        cfg["env"] = {}
    if not isinstance(cfg["headers"], dict):
        cfg["headers"] = {}
    cfg["headers"] = {str(k): str(v) for k, v in cfg["headers"].items()}
    cfg["sandbox"] = bool(cfg["sandbox"])
    cfg["sandbox_image"] = str(cfg["sandbox_image"] or "agent-sandbox").strip()
    if not isinstance(cfg["sandbox_mounts"], list):
        cfg["sandbox_mounts"] = []
    cfg["sandbox_mounts"] = [str(m) for m in cfg["sandbox_mounts"] if str(m).strip()]
    if not isinstance(cfg["keywords"], list):
        cfg["keywords"] = []
    cfg["keywords"] = [str(k) for k in cfg["keywords"] if str(k).strip()]
    try:
        cfg["timeout"] = max(1, min(int(cfg["timeout"]), 300))
    except (TypeError, ValueError):
        cfg["timeout"] = DEFAULT_SERVER["timeout"]
    return cfg


def validate(name: str, raw: dict) -> tuple[dict | None, str | None]:
    """校验并归一化。返回 (配置, 错误)。"""
    if not NAME_RE.match(name or ""):
        return None, "名称只能用字母/数字/下划线/短横线，长度 1-32，且不能以符号开头"

    cfg = normalize(name, raw)

    if cfg["transport"] == "stdio":
        if not cfg["command"].strip():
            return None, "stdio 类型必须填 command（例如 npx / uvx / python）"
        if not shutil.which(cfg["command"]) and not os.path.isabs(cfg["command"]):
            return None, f"找不到可执行文件：{cfg['command']}"
    else:
        if not str(cfg["url"]).startswith(("http://", "https://")):
            return None, "URL 必须以 http:// 或 https:// 开头"

    if cfg["sandbox"]:
        for m in cfg["sandbox_mounts"]:
            if not os.path.isdir(m):
                return None, f"沙箱挂载目录不存在：{m}"
        if cfg["transport"] != "stdio":
            return None, "只有 stdio 类型的插件支持沙箱运行"

    shadow = shadowing_name(name)
    if shadow:
        return None, f"名称「{name}」会与内置工具冲突，请换一个"
    return cfg, None


def shadowing_name(name: str) -> str | None:
    """检查 server 名是否和内置工具重名（防止劫持看起来安全的工具名）。"""
    try:
        from tools import TOOL_FUNCTIONS
    except Exception:
        return None
    return name if name in TOOL_FUNCTIONS else None


def list_servers() -> list[dict]:
    cfg = load_config()
    out = []
    for name, raw in sorted(cfg["servers"].items()):
        item = normalize(name, raw)
        item["configured"] = True
        out.append(item)
    return out


def get_server(name: str) -> dict | None:
    raw = load_config()["servers"].get(name)
    return normalize(name, raw) if raw is not None else None


def upsert_server(name: str, raw: dict) -> None:
    cfg = load_config()
    cfg["servers"][name] = normalize(name, raw)
    save_config(cfg)


def patch_server(name: str, **fields) -> dict | None:
    """只改几个字段（例如只切换 enabled）。"""
    cfg = load_config()
    if name not in cfg["servers"]:
        return None
    merged = dict(cfg["servers"][name])
    merged.update(fields)
    cfg["servers"][name] = normalize(name, merged)
    save_config(cfg)
    return cfg["servers"][name]


def delete_server(name: str) -> bool:
    cfg = load_config()
    if name not in cfg["servers"]:
        return False
    cfg["servers"].pop(name)
    save_config(cfg)
    return True


def set_inject_enabled(flag: bool) -> None:
    """设置面板里那个「启用 MCP 工具注入」总开关。"""
    cfg = load_config()
    cfg["inject_enabled"] = bool(flag)
    save_config(cfg)


def server_env(cfg: dict) -> dict:
    """构造交给 MCP server 的环境变量（白名单 + 用户显式配置）。"""
    env = {k: v for k, v in os.environ.items() if k in ENV_WHITELIST}
    env.update({str(k): str(v) for k, v in (cfg.get("env") or {}).items()})
    return env
