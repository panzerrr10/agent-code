"""Docker 沙箱：把「执行类」工具统一收进容器。

**与早期实现的区别**

早期用 ``docker exec agent-sandbox``，容器挂载的是 ``~/ai_workspace``，
而文件类工具（read/write/edit）用的是 ``project_root`` —— 两者对 ``/workspace``
的理解并不一致，``run_python`` 里写的文件在文件工具看来根本不存在。

现在改为**每次调用都 ``docker run --rm`` 并挂载真实的 project_root**：
沙箱内看到的目录与文件工具完全一致，且每次执行都是干净容器。

**默认强化项**

- ``--network=none``：容器内即便用 ``requests`` 也传不出去，堵死数据外传通道
- ``--cap-drop=ALL`` + ``--security-opt=no-new-privileges``：收敛内核能力
- ``--pids-limit`` / ``--memory`` / ``--cpus``：限制资源，防 fork 炸弹与挖矿
- ``--user uid:gid``：容器内以当前用户身份写文件，产物归属不变

**回退策略（刻意区分）**

- ``run_python`` / ``run_command``（**任意代码**）：Docker 不可用时**拒绝执行**，
  绝不悄悄落到宿主机；只有显式设置 ``AGENT_ALLOW_HOST_EXEC=1`` 才允许宿主执行。
- ``git_*``（受控命令）：优先沙箱；Docker 不可用时回退宿主机，并在输出里标注。
- ``run_tests``：默认宿主机（测试需要项目自身的虚拟环境与依赖），
  可用 ``AGENT_TESTS_IN_SANDBOX=1`` 切到沙箱。
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess

logger = logging.getLogger(__name__)

IMAGE = os.getenv("AGENT_SANDBOX_IMAGE", "agent-sandbox")
NETWORK = os.getenv("AGENT_SANDBOX_NETWORK", "none")
MEMORY = os.getenv("AGENT_SANDBOX_MEMORY", "512m")
CPUS = os.getenv("AGENT_SANDBOX_CPUS", "1")
# 显式允许「任意代码」在宿主机上跑（默认关闭，安全兜底）
ALLOW_HOST_EXEC = os.getenv("AGENT_ALLOW_HOST_EXEC") == "1"
# run_tests 默认在宿主机跑（需要项目自己的 venv / 依赖）
TESTS_IN_SANDBOX = os.getenv("AGENT_TESTS_IN_SANDBOX") == "1"

_MOUNT_TARGET = "/workspace"

_docker_ok: bool | None = None
_image_ok: bool | None = None


def reset_cache() -> None:
    """测试 / 运维用：清掉 docker 可用性缓存，下次重新探测。"""
    global _docker_ok, _image_ok
    _docker_ok = None
    _image_ok = None


def docker_available() -> bool:
    global _docker_ok
    if _docker_ok is None:
        try:
            r = subprocess.run(
                ["docker", "info"], capture_output=True, timeout=5, text=True
            )
            _docker_ok = r.returncode == 0
        except Exception:
            _docker_ok = False
    return _docker_ok


def image_available() -> bool:
    global _image_ok
    if _image_ok is None:
        try:
            r = subprocess.run(
                ["docker", "image", "inspect", IMAGE],
                capture_output=True,
                timeout=10,
                text=True,
            )
            _image_ok = r.returncode == 0
        except Exception:
            _image_ok = False
    return _image_ok


class SandboxResult:
    """沙箱执行结果。

    ``ran`` 为 True 表示「已经在容器里跑过了，调用方不要再执行」；
    为 False 表示沙箱不可用、且策略允许回退宿主机。
    """

    __slots__ = ("ran", "output")

    def __init__(self, ran: bool, output: str = ""):
        self.ran = ran
        self.output = output


def _build_docker_cmd(project_root: str, script: str) -> list[str]:
    cmd = [
        "docker",
        "run",
        "--rm",
        "--network",
        NETWORK,
        "--memory",
        MEMORY,
        "--cpus",
        CPUS,
        "--pids-limit",
        "256",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "-e",
        "HOME=/tmp",
        "-v",
        f"{project_root}:{_MOUNT_TARGET}",
        "-w",
        _MOUNT_TARGET,
    ]
    # 宿主机的 git 身份配置（如果存在）只读挂进去，避免容器内 commit 因缺少
    # user.name/user.email 而失败
    gitconfig = os.path.join(os.path.expanduser("~"), ".gitconfig")
    if os.path.isfile(gitconfig):
        cmd += [
            "-v",
            f"{gitconfig}:/tmp/.gitconfig:ro",
            "-e",
            "GIT_CONFIG_GLOBAL=/tmp/.gitconfig",
        ]
    cmd += [IMAGE, "sh", "-c", script]
    return cmd


def run_script(
    script: str,
    timeout: int,
    project_root: str,
    *,
    allow_host_fallback: bool = False,
) -> SandboxResult:
    """在沙箱里执行一段 shell 脚本。

    allow_host_fallback=False（默认，用于任意代码执行）：
        Docker 不可用时直接返回拒绝信息，绝不在宿主机跑。
    allow_host_fallback=True（用于受控命令如 git）：
        返回 ran=False，由调用方在宿主机执行。
    """
    if not docker_available():
        # 允许回退的情形：受控命令（git）或用户显式开启了宿主机执行
        if allow_host_fallback or ALLOW_HOST_EXEC:
            return SandboxResult(False)
        return SandboxResult(
            True,
            "❌ 当前环境未启用 Docker 沙箱，已拒绝执行任意代码。\n"
            f"   请安装并启动 Docker 后重试：docker build -t {IMAGE} .\n"
            "   （确有需要可设置 AGENT_ALLOW_HOST_EXEC=1 允许在宿主机执行，风险自负）",
        )

    if not image_available():
        return SandboxResult(
            True,
            f"❌ 找不到沙箱镜像 `{IMAGE}`，无法安全执行。\n"
            f"   请先构建：docker build -t {IMAGE} .",
        )

    cmd = _build_docker_cmd(project_root, script)
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return SandboxResult(True, f"❌ 执行超时（>{timeout} 秒），容器已终止")
    except Exception as e:  # pragma: no cover - 依赖本机 docker
        return SandboxResult(True, f"❌ 沙箱执行失败：{e}")

    out = ""
    if r.stdout:
        out += f"【stdout】\n{r.stdout}"
    if r.stderr:
        out += f"\n【stderr】\n{r.stderr}"
    out += f"\n\n【退出码】{r.returncode}\n【沙箱】docker:{IMAGE}（network={NETWORK}）"
    return SandboxResult(True, out)


def run_argv(
    argv: list[str],
    timeout: int,
    project_root: str,
    *,
    allow_host_fallback: bool = False,
) -> SandboxResult:
    """把一个命令（argv 列表）安全地转成 shell 脚本后送进沙箱。

    每个参数都用 shlex.quote 转义，因此 argv 里的引号/分号不会被 shell 解释，
    仍然保持「参数列表」的安全性。
    """
    script = " ".join(shlex.quote(a) for a in argv)
    return run_script(
        script, timeout, project_root, allow_host_fallback=allow_host_fallback
    )


HOST_EXEC_NOTICE = "⚠️ 未启用 Docker 沙箱，以下命令直接在宿主机执行（风险自负）\n"
