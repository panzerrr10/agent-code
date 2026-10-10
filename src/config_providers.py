"""大模型提供商配置 —— **全部由用户在界面里定义，代码里不再写死**。

## 数据在哪

``data/providers.json``（权限 600，已被 .gitignore 覆盖）::

    {
      "providers": {
        "deepseek": {
          "id": "deepseek", "name": "DeepSeek", "icon": "🐳",
          "base_url": "https://api.deepseek.com/v1",
          "api_key": "sk-...",
          "models": ["deepseek-chat", "deepseek-reasoner"],
          "default_model": "deepseek-chat",
          "supports_embedding": false
        }
      },
      "default_provider": "deepseek",
      "rag_provider": "deepseek"
    }

## 两条铁律

1. **密钥只进不出**：``get_provider_list()`` 是给前端的，只回 ``has_key`` 与掩码
   ``sk-…abcd``，**任何接口都不回传完整密钥**。
2. **代码里没有预设提供商**。以前这里写着 DeepSeek / 百炼 / 智谱 / OpenAI 四家的
   base_url 与模型列表 —— 想加一家就得改代码。现在用户想加几家加几家。

## 关于 ``_LEGACY_PRESETS``

它**不参与运行时**，只在升级时用一次（见 ``migrate_legacy_env``）：
老用户的可跑配置全在 ``.env`` 里，直接把预设删干净会让应用立刻不可用。
所以第一次读配置时，把 ``.env`` 里**确实配了 key** 的那几家，
落成普通的用户自定义提供商 —— 之后它们和用户手加的没有区别，随时可改可删。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time

from dotenv import load_dotenv

from paths import DATA_DIR

# 本模块在 import 时就要用 os.getenv 做迁移判断，因此必须自己先加载 .env。
load_dotenv()

logger = logging.getLogger(__name__)

SETTINGS_FILE = os.environ.get("AGENT_SETTINGS_PATH") or os.path.join(
    DATA_DIR, "providers.json"
)

# 仅供一次性迁移使用（不参与运行时）。
_LEGACY_PRESETS = {
    "deepseek": {
        "name": "DeepSeek", "icon": "🐳", "env": "DEEPSEEK_API_KEY",
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "default_model": "deepseek-chat", "supports_embedding": False,
    },
    "bailian": {
        "name": "阿里云百炼", "icon": "☁️", "env": "DASHSCOPE_API_KEY",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-max", "qwen-plus", "qwen-turbo"],
        "default_model": "qwen-plus", "supports_embedding": True,
    },
    "zhipu": {
        "name": "智谱 AI", "icon": "🔮", "env": "ZHIPU_API_KEY",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-4-plus", "glm-4", "glm-4-flash"],
        "default_model": "glm-4-plus", "supports_embedding": False,
    },
    "openai": {
        "name": "OpenAI", "icon": "🤖", "env": "OPENAI_API_KEY",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o", "gpt-4o-mini"],
        "default_model": "gpt-4o-mini", "supports_embedding": True,
    },
}

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")


# ---------------------------------------------------------------- 密钥掩码


def key_hint(api_key: str) -> str:
    """把密钥变成可展示的掩码：sk-…abcd。

    **永远不要在接口里回传完整密钥** —— 浏览器拿得到就等于泄漏。
    """
    if not api_key:
        return ""
    if len(api_key) <= 10:
        return "•" * len(api_key)
    return f"{api_key[:3]}…{api_key[-4:]}"


def mask_placeholder(key: str) -> bool:
    """判断是不是 .env.example 那种占位符（不算「已配置」）。"""
    if not key:
        return True
    return any(kw.lower() in key.lower() for kw in ("你的", "your", "placeholder", "xxx"))


# ---------------------------------------------------------------- 读写


def _empty() -> dict:
    return {"providers": {}, "default_provider": "", "rag_provider": ""}


def load_config() -> dict:
    """读取配置。文件不存在/损坏时按空配置处理，并尝试一次 .env 迁移。"""
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except FileNotFoundError:
        data = {}
    except Exception:
        logger.warning("读取 %s 失败，按空配置处理", SETTINGS_FILE, exc_info=True)
        data = {}

    migrated = False
    if "providers" not in data:
        # 旧格式（v1 的「覆盖项」）或全新安装 → 迁移
        data = migrate_legacy_env(data)
        migrated = True

    data.setdefault("providers", {})
    data.setdefault("default_provider", "")
    data.setdefault("rag_provider", "")
    if not isinstance(data["providers"], dict):
        data["providers"] = {}

    if migrated:
        try:
            save_config(data)
            if data["providers"]:
                logger.info(
                    "已从 .env 迁移 %d 个提供商到 %s（之后可在界面里随意改/删）",
                    len(data["providers"]), SETTINGS_FILE,
                )
        except Exception:
            logger.warning("迁移结果写入失败", exc_info=True)
    return data


def save_config(cfg: dict) -> None:
    os.makedirs(os.path.dirname(SETTINGS_FILE) or ".", exist_ok=True)
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS_FILE)
    try:
        os.chmod(SETTINGS_FILE, 0o600)     # 里面有密钥，只给本人读写
    except OSError:
        pass


def migrate_legacy_env(old: dict) -> dict:
    """把老配置（.env 里的 key + 旧预设）落成普通的用户提供商。

    - 只迁移**确实配了可用 key** 的（占位符不算）
    - 旧 ``providers.json`` 里界面填过的 key / url 优先
    - 智谱/OpenAI 那种没配 key 的不迁移 —— 免得界面上多出两个用不了的条目
    """
    cfg = _empty()
    old = old if isinstance(old, dict) else {}

    for pid, preset in _LEGACY_PRESETS.items():
        override = old.get(pid) if isinstance(old.get(pid), dict) else {}
        env_key = os.getenv(preset["env"], "")
        api_key = (override.get("api_key") or env_key or "").strip()
        base_url = (override.get("base_url") or preset["base_url"] or "").strip()
        if mask_placeholder(api_key):
            continue
        cfg["providers"][pid] = {
            "id": pid,
            "name": preset["name"],
            "icon": preset["icon"],
            "base_url": base_url,
            "api_key": api_key,
            "models": list(preset["models"]),
            "default_model": preset["default_model"],
            "supports_embedding": bool(preset["supports_embedding"]),
        }

    if cfg["providers"]:
        # 默认用老代码里的默认项；不在迁移结果里就取第一个
        cfg["default_provider"] = (
            "deepseek" if "deepseek" in cfg["providers"] else next(iter(cfg["providers"]))
        )
        # RAG 优先继承老行为（百炼），否则退到一个标了 embedding 的
        if "bailian" in cfg["providers"]:
            cfg["rag_provider"] = "bailian"
        else:
            cfg["rag_provider"] = next(
                (k for k, v in cfg["providers"].items() if v.get("supports_embedding")), "",
            )
    return cfg


# ---------------------------------------------------------------- 归一化与校验


def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return s[:32]


def normalize(pid: str, raw: dict) -> dict:
    """补齐字段、裁掉非法值。"""
    raw = raw if isinstance(raw, dict) else {}
    models = raw.get("models") or []
    if isinstance(models, str):
        models = [m.strip() for m in re.split(r"[,\n]", models) if m.strip()]
    models = [str(m).strip() for m in models if str(m).strip()]
    # 去重但保序
    seen, uniq = set(), []
    for m in models:
        if m not in seen:
            seen.add(m)
            uniq.append(m)
    models = uniq

    default_model = str(raw.get("default_model") or "").strip()
    if not default_model or default_model not in models:
        default_model = models[0] if models else ""

    return {
        "id": pid,
        "name": str(raw.get("name") or "").strip() or pid,
        "icon": (str(raw.get("icon") or "🧠").strip() or "🧠")[:2],
        "base_url": str(raw.get("base_url") or "").strip(),
        "api_key": str(raw.get("api_key") or "").strip(),
        "models": models,
        "default_model": default_model,
        "supports_embedding": bool(raw.get("supports_embedding")),
    }


def validate(pid: str, raw: dict) -> tuple[dict | None, str | None]:
    """校验并归一化。返回 (provider, 错误)。"""
    if not _ID_RE.match(pid or ""):
        return None, "提供商标识只能用字母/数字/下划线/短横线，长度 1-32"
    p = normalize(pid, raw)
    if not str(raw.get("name") or "").strip():
        return None, "请填写名称"
    if not p["base_url"]:
        return None, "请填写 Base URL"
    if not p["base_url"].startswith(("http://", "https://")):
        return None, "Base URL 必须以 http:// 或 https:// 开头"
    if not p["models"]:
        return None, "请至少填写一个模型名称"
    return p, None


def make_id(name: str, existing) -> str:
    """按名称生成一个不冲突的标识。"""
    base = _slug(name) or f"p{int(time.time())}"
    if base not in existing:
        return base
    for i in range(2, 100):
        cand = f"{base}-{i}"
        if cand not in existing:
            return cand
    return f"p{int(time.time())}"


# ---------------------------------------------------------------- 查询


def list_providers() -> list:
    """全部提供商（**含完整密钥**）—— 只给后端用。"""
    cfg = load_config()
    return [normalize(pid, raw) for pid, raw in cfg["providers"].items()]


def get_provider_list() -> list:
    """给前端的列表：**不含完整密钥**，只有 has_key 与掩码。"""
    out = []
    for p in list_providers():
        key = p.get("api_key") or ""
        usable = not mask_placeholder(key)
        out.append({
            "id": p["id"],
            "name": p["name"],
            "icon": p["icon"],
            "base_url": p["base_url"],
            "models": p["models"],
            "default_model": p["default_model"],
            "supports_embedding": p["supports_embedding"],
            "has_key": usable,
            "key_hint": key_hint(key) if usable else "",
        })
    return out


def provider_exists(pid: str) -> bool:
    return bool(pid) and pid in load_config()["providers"]


def get_default_provider_id() -> str:
    """默认提供商的 id。没设置就取第一个；一个都没有则返回空串。"""
    cfg = load_config()
    ids = list(cfg["providers"].keys())
    if not ids:
        return ""
    dp = cfg.get("default_provider") or ""
    return dp if dp in ids else ids[0]


def get_rag_provider_id() -> str:
    """RAG（向量检索）用哪个提供商的嵌入能力。"""
    cfg = load_config()
    ids = list(cfg["providers"].keys())
    if not ids:
        return ""
    rp = cfg.get("rag_provider") or ""
    if rp in ids:
        return rp
    # 退到一个声明了支持 embedding 的
    for pid in ids:
        if normalize(pid, cfg["providers"][pid]).get("supports_embedding"):
            return pid
    return get_default_provider_id()


def get_provider(provider_id: str = None) -> dict:
    """取某个提供商的完整配置（**含 api_key**）。

    返回的是**副本**：调用方改它不会污染配置文件。
    传空则用默认提供商；一个都没配时返回一个空壳并给出 warning，
    让上层能报出可理解的错误而不是 KeyError。
    """
    cfg = load_config()
    pid = provider_id or get_default_provider_id()
    raw = cfg["providers"].get(pid)
    if raw is None:
        if cfg["providers"] and provider_id:
            logger.warning("未知的 provider_id=%r，回退到默认", provider_id)
            return get_provider(None)
        logger.warning("还没有配置任何大模型提供商（请在设置 → 模型与密钥 里添加）")
        return {"id": "", "name": "", "icon": "🧠", "base_url": "", "api_key": "",
                "models": [], "default_model": "", "supports_embedding": False}
    p = normalize(pid, raw)
    if mask_placeholder(p.get("api_key")):
        logger.warning("provider=%r 还没有可用的 api_key", pid)
    return p


def get_provider_id_by_base_url(base_url: str) -> str:
    """按 base_url 反查是哪个提供商（用于 CLI 没显式传 provider 的场景）。"""
    if base_url:
        for p in list_providers():
            url = p.get("base_url") or ""
            if url and (url in base_url or base_url in url):
                return p["id"]
    return get_default_provider_id()


def get_extra_body(provider_id: str) -> dict:
    """厂商专属的 extra_body 参数。

    只有 DeepSeek 支持 ``enable_search``；别家收到未知参数会直接 400，
    所以必须按提供商条件化下发，不能全局写死。
    **按 base_url 判断而不是按 id** —— 用户自定义提供商时 id 是随便起的。
    """
    p = get_provider(provider_id) if provider_id else get_provider()
    base_url = (p.get("base_url") or "").lower()
    if "deepseek" in base_url:
        return {"enable_search": True, "search_options": {"search_strategy": "turbo"}}
    return {}


def get_rag_config() -> dict:
    """RAG 专用配置（含 api_key / base_url）。

    优先用 ``rag_provider`` 指定的那家；``.env`` 里的 RAG_API_KEY / RAG_BASE_URL
    仍然作为独立覆盖（老部署兼容）。
    """
    config = dict(get_provider(get_rag_provider_id()))
    override_key = os.getenv("RAG_API_KEY")
    if override_key:
        config["api_key"] = override_key
    override_base_url = os.getenv("RAG_BASE_URL")
    if override_base_url:
        config["base_url"] = override_base_url
    return config


# ---------------------------------------------------------------- 增删改


def add_provider(data: dict) -> tuple[dict | None, str | None]:
    """新增一个提供商。``id`` 可省略，按名称自动生成。"""
    cfg = load_config()
    name = str((data or {}).get("name") or "").strip()
    pid = str((data or {}).get("id") or "").strip() or make_id(name, cfg["providers"])
    if pid in cfg["providers"]:
        return None, f"标识「{pid}」已存在"
    p, err = validate(pid, data)
    if err:
        return None, err
    cfg["providers"][pid] = p
    if not cfg.get("default_provider"):
        cfg["default_provider"] = pid
    save_config(cfg)
    return p, None


def update_provider(pid: str, data: dict) -> tuple[dict | None, str | None]:
    """修改一个提供商。

    ``api_key`` 传空表示**不修改**（界面上永远不回显密钥，留空即保持原值）。
    """
    cfg = load_config()
    if pid not in cfg["providers"]:
        return None, f"没有这个提供商：{pid}"
    cur = normalize(pid, cfg["providers"][pid])
    merged = dict(cur)
    merged.update({k: v for k, v in (data or {}).items() if k in cur and k != "id"})
    if not str((data or {}).get("api_key") or "").strip():
        merged["api_key"] = cur["api_key"]          # 留空 = 不改密钥
    if not str((data or {}).get("name") or "").strip():
        merged["name"] = cur["name"]
    p, err = validate(pid, merged)
    if err:
        return None, err
    cfg["providers"][pid] = p
    save_config(cfg)
    return p, None


def delete_provider(pid: str) -> bool:
    cfg = load_config()
    if pid not in cfg["providers"]:
        return False
    cfg["providers"].pop(pid)
    # 兜底：默认 / RAG 指向被删的那家时要改指
    if cfg.get("default_provider") == pid:
        cfg["default_provider"] = next(iter(cfg["providers"]), "")
    if cfg.get("rag_provider") == pid:
        cfg["rag_provider"] = ""
    save_config(cfg)
    return True


def set_default_provider(pid: str) -> bool:
    cfg = load_config()
    if pid not in cfg["providers"]:
        return False
    cfg["default_provider"] = pid
    save_config(cfg)
    return True


def set_rag_provider(pid: str) -> bool:
    cfg = load_config()
    if pid and pid not in cfg["providers"]:
        return False
    cfg["rag_provider"] = pid
    save_config(cfg)
    return True
