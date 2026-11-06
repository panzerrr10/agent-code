"""执行策略开关 —— 联网 / 安装依赖 / 推送到远端。

## 为什么需要这个模块

原来这三件事被**硬编码**在 ``tools.BANNED_RULES`` 里一律拦死（「禁止一切外发」）。
对「给小白用的私人玩具」这是合理的默认；但作为程序员的日常工具，它直接让 Agent 残废：

- 装不了依赖 → 遇到 ``ModuleNotFoundError`` 就死
- ``git pull`` / ``git clone`` 被拦 → 只能在过期代码上干活
- ``git push`` 被拦 → 干完还得自己手动推

而且原来的设计**连「我知道风险，我要开」这个选项都没给**。本模块补上的就是这三个开关。

## 设计

- **默认值**（按用户要求）：
  * ``allow_package_install`` = **开** —— 开箱即可装依赖，否则遇到
    ``ModuleNotFoundError`` 就死，对日常开发不可用。
  * ``allow_network`` / ``allow_git_push`` = **关** —— ``curl`` / ``git pull``
    / ``git push`` 仍需显式授权。
  * ``allow_reviewer_tests`` = 开。
- 三层覆盖，优先级从高到低：
  1. 环境变量 ``AGENT_ALLOW_NETWORK`` / ``AGENT_ALLOW_PACKAGE_INSTALL`` /
     ``AGENT_ALLOW_GIT_PUSH`` / ``AGENT_ALLOW_REVIEWER_TESTS``
     （值为 ``1`` / ``true`` / ``yes`` 视为开启）—— 给 CLI、脚本和 Docker 用
  2. ``data/policy.json``（界面里改的，权限 0600）
  3. 内置默认（见上）
- 环境变量优先级**高于**文件：这样 ``AGENT_ALLOW_NETWORK=1 python src/cli.py`` 这种
  一次性用法不会被界面里的设置意外覆盖；反过来想改持久行为就去界面改。

## 与沙箱的关系

开了 ``allow_network`` 或 ``allow_package_install`` 之后，``sandbox`` 会自动把容器从
``--network=none`` 切到 ``bridge`` —— 否则命令放行了但容器里还是出不去，
用户会看到「明明开了还是失败」这种最费解的故障。见 ``policy.sandbox_network()``。

⚠️ 连带后果：因为 ``allow_package_install`` 默认开，**沙箱默认就是 bridge 网络**。
``curl`` / ``git pull`` 仍在命令层被拦，但 ``run_python`` 里的 ``requests``
能直接出网 —— 这一层不受 ``allow_network`` 约束。要回到「默认禁网」，
把 ``defaults()`` 里那一行改成 False。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

from dotenv import load_dotenv

from paths import DATA_DIR

# 本模块可能被 CLI 在 config.py 之前 import，所以自己先加载 .env
load_dotenv()

logger = logging.getLogger(__name__)

SETTINGS_FILE = os.environ.get("AGENT_POLICY_PATH") or os.path.join(DATA_DIR, "policy.json")

# 开关名 -> 环境变量名。加新开关时只要往这张表里加一行。
FLAGS = {
    "allow_network": "AGENT_ALLOW_NETWORK",
    # 安装依赖（pip / npm / yarn / pnpm / uv / apt）。
    #
    # **默认开**（按用户要求）：一个不能装依赖的编程 Agent，遇到
    # ModuleNotFoundError 就死，日常开发不可用。
    # ⚠️ 它是三家里唯一默认开的，连带把沙箱网络也顶成 bridge（见 sandbox_network）。
    "allow_package_install": "AGENT_ALLOW_PACKAGE_INSTALL",
    "allow_git_push": "AGENT_ALLOW_GIT_PUSH",
    # 评审阶段（Reviewer）自动跑测试。
    #
    # 为什么需要单独一个开关：Reviewer 是一个**无人值守**的小循环，
    # 没有确认通道 —— 它不可能弹一张卡片等你点勾。而 run_tests 属于
    # execute 档，按严格语义在等级 2 下要逐次确认。
    # 这里的选择是：把它做成一个**显式的、用户可见可关的授权**（默认开），
    # 而不是绕过权限模型。等级 1（只读）无论如何都不允许。
    # 关掉它就回到严格语义：评审只做静态审查，不跑测试。
    "allow_reviewer_tests": "AGENT_ALLOW_REVIEWER_TESTS",
}

_TRUTHY = {"1", "true", "yes", "on"}

_lock = threading.RLock()
_cache: dict | None = None
_cache_mtime: float | None = None
# 缓存时的环境变量取值快照（见 get_policy 的说明）
_cache_env: tuple | None = None


def defaults() -> dict:
    """默认值。

    - ``allow_network`` / ``allow_git_push`` 默认**关**。
    - ``allow_package_install`` 默认**开**（按用户要求）：Agent 开箱即可
      ``pip install`` / ``npm install``，否则遇到 `ModuleNotFoundError` 就死。
    - ``allow_reviewer_tests`` 默认**开**：它是「让评审能验证结果」这个新能力
      的总开关，关掉就回到「评审只做静态审查」。等级 1 下无论如何都不允许。

    ⚠️ **默认开 ``allow_package_install`` 有一个必须知道的连带后果**：
    ``sandbox_network()`` 在「联网或安装」任一打开时就给容器 ``bridge`` 网络
    （否则装了也下载不到）。所以此项默认开 = **沙箱默认有网**，
    尽管 ``allow_network`` 仍是关的、``curl`` / ``git pull`` 仍在命令层被拦。
    容器有网意味着 ``run_python`` 里的 ``requests`` 能出网 —— 这一层不受
    ``allow_network`` 约束。想要回到「默认禁网」，把这一行改成 False 即可。
    """
    policy = dict.fromkeys(FLAGS, False)
    policy["allow_package_install"] = True
    policy["allow_reviewer_tests"] = True
    return policy


def _env_override(flag: str) -> bool | None:
    """环境变量是否显式设置了该开关。没设置返回 None（表示「不由我决定」）。"""
    raw = os.getenv(FLAGS[flag])
    if raw is None or not raw.strip():
        return None
    return raw.strip().lower() in _TRUTHY


def _read_file() -> dict:
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:                                        # noqa: BLE001
        logger.warning("读取 %s 失败，回退到默认策略", SETTINGS_FILE, exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


def _mtime() -> float | None:
    try:
        return os.path.getmtime(SETTINGS_FILE)
    except OSError:
        return None


def get_policy() -> dict:
    """返回当前生效的策略（合并 环境变量 > 文件 > 默认）。

    缓存失效看**两样东西**：
    - 文件 mtime（CLI 改完，Web 进程也能读到新值）
    - 当前环境变量的取值快照（``AGENT_ALLOW_*`` 改了要立刻生效）

    只按 mtime 缓存是不够的：环境变量在进程生命周期内被改（测试、
    ``os.environ`` 注入、同一进程里切模式）时，光看 mtime 会一直吐旧值 ——
    表现为「明明设了 AGENT_ALLOW_NETWORK=0，沙箱还是 bridge」这种鬼故事。
    """
    global _cache, _cache_mtime, _cache_env

    with _lock:
        mtime = _mtime()
        env_state = tuple(_env_override(flag) for flag in FLAGS)
        if _cache is not None and mtime == _cache_mtime and env_state == _cache_env:
            return dict(_cache)

        policy = defaults()
        on_disk = _read_file()
        for flag in FLAGS:
            if flag in on_disk:
                policy[flag] = bool(on_disk[flag])
        for flag, override in zip(FLAGS, env_state, strict=True):
            if override is not None:
                policy[flag] = override

        _cache = policy
        _cache_mtime = mtime
        _cache_env = env_state
        return dict(policy)


def is_enabled(flag: str) -> bool:
    if flag not in FLAGS:
        raise KeyError(f"未知策略开关：{flag}")
    return bool(get_policy()[flag])


def allow_network() -> bool:
    """是否允许联网类命令（curl / wget / git fetch / pull / clone）。"""
    return is_enabled("allow_network")


def allow_package_install() -> bool:
    """是否允许包管理器安装（pip / npm / yarn / pnpm / apt …）。"""
    return is_enabled("allow_package_install")


def allow_git_push() -> bool:
    """是否允许把代码推到远端。**与 allow_network 分开** —— 它会把本地代码发出去。"""
    return is_enabled("allow_git_push")


def sandbox_network() -> str:
    """容器该用哪个网络模式。

    ``AGENT_SANDBOX_NETWORK`` 显式设置时以它为准（保持老配置可控）；
    否则只要开了联网或安装，就必须给容器网络，不然命令放行了也跑不通。
    """
    explicit = os.getenv("AGENT_SANDBOX_NETWORK")
    if explicit and explicit.strip():
        return explicit.strip()
    if allow_network() or allow_package_install():
        return "bridge"
    return "none"


def set_policy(payload: dict) -> dict:
    """更新开关并落盘。只接受 FLAGS 里的键，忽略其余（防前端塞垃圾字段）。

    写盘用「临时文件 + os.replace」，避免写一半留下坏文件。
    """
    if not isinstance(payload, dict):
        raise ValueError("payload 必须是 dict")

    with _lock:
        current = dict(get_policy())
        changed = False
        for flag in FLAGS:
            if flag in payload:
                value = bool(payload[flag])
                if current[flag] != value:
                    changed = True
                current[flag] = value

        if changed:
            merged = _read_file()
            for flag in FLAGS:
                merged[flag] = current[flag]
            _write(merged)

        # 无论是否变化都重置缓存：环境变量与文件可能同时变过
        _invalidate()
    return get_policy()


def _write(data: dict) -> None:
    os.makedirs(os.path.dirname(SETTINGS_FILE) or ".", exist_ok=True)
    body = json.dumps(data, ensure_ascii=False, indent=2)
    tmp = SETTINGS_FILE + f".tmp{os.getpid()}"
    # 以 0600 创建，不留「先 0644 再 chmod」的窗口
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp, SETTINGS_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        os.chmod(SETTINGS_FILE, 0o600)
    except OSError:                                          # pragma: no cover
        pass


def _invalidate() -> None:
    global _cache, _cache_mtime, _cache_env
    _cache = None
    _cache_mtime = None
    _cache_env = None


def reset_cache() -> None:
    """测试用：丢弃进程内缓存，下次调用重新读盘 / 读环境变量。"""
    with _lock:
        _invalidate()


def snapshot() -> dict:
    """给界面看的完整状态：当前值 + 是否被环境变量锁住 + 存盘路径。"""
    policy = get_policy()
    return {
        "policy": policy,
        "locked_by_env": {
            flag: _env_override(flag) is not None for flag in FLAGS
        },
        "sandbox_network": sandbox_network(),
        "file": SETTINGS_FILE,
        "checked_at": time.time(),
    }
