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
import tempfile
import time
import uuid

logger = logging.getLogger(__name__)

IMAGE = os.getenv("AGENT_SANDBOX_IMAGE", "agent-sandbox")
# 网络模式**不再在 import 时定死**：它要跟着 policy 走 —— 用户打开「允许联网 /
# 允许安装依赖」之后，容器必须同时拿到网络，否则命令放行了但容器里还是出不去，
# 用户会看到「明明开了还是失败」这种最费解的故障。
# 这里保留同名常量仅作**默认值**，真正生效的取 network_mode()。
NETWORK = os.getenv("AGENT_SANDBOX_NETWORK", "none")
MEMORY = os.getenv("AGENT_SANDBOX_MEMORY", "512m")
CPUS = os.getenv("AGENT_SANDBOX_CPUS", "1")
# 显式允许「任意代码」在宿主机上跑（默认关闭，安全兜底）
ALLOW_HOST_EXEC = os.getenv("AGENT_ALLOW_HOST_EXEC") == "1"
# run_tests 默认在宿主机跑（需要项目自己的 venv / 依赖）
TESTS_IN_SANDBOX = os.getenv("AGENT_TESTS_IN_SANDBOX") == "1"

_MOUNT_TARGET = "/workspace"


def network_mode() -> str:
    """本次容器该用哪个网络模式（每次调用都重新判定，设置改了立刻生效）。"""
    try:
        import policy

        return policy.sandbox_network()
    except Exception:                                        # noqa: BLE001
        return NETWORK

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


def _build_docker_cmd(
    project_root: str, script: str, name: str | None = None
) -> list[str]:
    cmd = [
        "docker",
        "run",
        "--rm",
    ]
    if name:
        # 唯一名字，便于超时/中断时 docker rm -f 精准清理
        cmd += ["--name", name]
    cmd += [
        "--network",
        network_mode(),
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
    cmd += _build_cache_mounts()
    cmd += [IMAGE, "sh", "-c", script]
    return cmd


# 宿主机缓存目录 -> 容器内 HOME(/tmp) 下的对应位置。
#
# 为什么需要：容器每次都是 `docker run --rm` 起的**全新**环境，`HOME=/tmp`。
# 不挂缓存的话，每个 Java 项目都要把 Maven 依赖（常常几百 MB）重新下一遍，
# pip 同理。挂上之后就与宿主共用缓存 —— 重复构建快得多，也省流量。
#
# 只挂**已存在**的目录（`:rw`，因为容器要往里写）。不存在就不挂，行为与以前一致。
# 缓存的家目录。默认是用户 home，但允许用 AGENT_CACHE_HOME 改掉 ——
# 测试必须隔离它，否则跑一次用例就会在真实 home 里建出 ~/.m2
# （与本项目已经隔离 DB / 回收站 / MCP 配置 / 策略文件的理由一样）。
_CACHE_HOME = os.environ.get("AGENT_CACHE_HOME") or os.path.expanduser("~")

_CACHE_MOUNTS = (
    (".m2", "/tmp/.m2"),                          # Maven 本地仓库
    (os.path.join(".cache", "pip"), "/tmp/.cache/pip"),   # pip wheel 缓存
    (os.path.join(".cache", "uv"), "/tmp/.cache/uv"),     # uv 缓存
)


def _build_cache_mounts() -> list[str]:
    """给容器挂上宿主机的包管理器缓存。

    目录不存在就**建出来**（`~/.m2` 本来就是 Maven 第一次运行会自己建的）。
    不建的话这些缓存挂不进去，而容器每次都是 `--rm` 全新环境 ——
    每个 Java 项目都要把 Maven 依赖重下一遍，慢到必然撞超时。
    建出来的缓存同时被宿主的 mvn / pip 复用，重复构建快得多。

    挂载失败（权限等）不影响执行：调用方把它当尽力而为。
    """
    out: list[str] = []
    home = _CACHE_HOME
    for rel, target in _CACHE_MOUNTS:
        host = os.path.join(home, rel)
        if not os.path.isdir(host):
            try:
                os.makedirs(host, exist_ok=True)
            except OSError:
                continue
        # 必须是**我们写得进去**的目录。
        # 否则容器里以当前 uid 运行却写不进去，Maven 会抛
        # LocalRepositoryNotAccessibleException 这种看不懂的错。
        # 真实踩过：手动 `docker run -v ~/.m2:...` 时若源路径不存在，
        # Docker 守护进程会**以 root 建出**该目录，之后我们自己就用不了了。
        if not os.access(host, os.W_OK | os.X_OK):
            logger.warning("缓存目录不可写，跳过挂载（不影响执行，只是会慢）：%s", host)
            continue
        out += ["-v", f"{host}:{target}"]
    return out


def _kill_container(name: str) -> None:
    """强制删除容器（超时 / 用户中断时用）。

    必须显式 `docker rm -f`：`subprocess` 只能 kill 掉 docker **客户端**进程，
    客户端被 SIGKILL 时无法把信号转发给容器，容器会继续跑下去。
    """
    try:
        subprocess.run(
            ["docker", "rm", "-f", name], capture_output=True, timeout=20, check=False
        )
    except Exception:  # pragma: no cover - 依赖本机 docker
        logger.warning("清理沙箱容器失败：%s", name, exc_info=True)


def run_script(
    script: str,
    timeout: int,
    project_root: str,
    *,
    allow_host_fallback: bool = False,
    cancel_event=None,
) -> SandboxResult:
    """在沙箱里执行一段 shell 脚本。

    allow_host_fallback=False（默认，用于任意代码执行）：
        Docker 不可用时直接返回拒绝信息，绝不在宿主机跑。
    allow_host_fallback=True（用于受控命令如 git）：
        返回 ran=False，由调用方在宿主机执行。
    cancel_event：threading.Event。置位后会**立刻**终止容器并返回，
        因此「用户点停止」不需要等工具自然跑完（原先最长要等 run_tests 的 120 秒）。
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

    # 唯一容器名：便于超时/中断时精准 docker rm -f（也支持并发运行）
    name = f"agent-exec-{uuid.uuid4().hex[:12]}"
    cmd = _build_docker_cmd(project_root, script, name=name)

    # 输出重定向到临时文件而不是 PIPE：容器输出可能超过管道缓冲区，
    # 用 PIPE + poll 会因缓冲区写满而死锁。
    with tempfile.TemporaryFile("w+", encoding="utf-8") as fo, tempfile.TemporaryFile(
        "w+", encoding="utf-8"
    ) as fe:
        try:
            proc = subprocess.Popen(cmd, stdout=fo, stderr=fe, text=True)
        except Exception as e:  # pragma: no cover - 依赖本机 docker
            return SandboxResult(True, f"❌ 沙箱执行失败：{e}")

        deadline = time.monotonic() + timeout
        cancelled = False
        timed_out = False
        while proc.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            if time.monotonic() > deadline:
                timed_out = True
                break
            time.sleep(0.2)

        if cancelled or timed_out:
            _kill_container(name)
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=10)
            except Exception:
                pass

        rc = proc.returncode
        fo.seek(0)
        fe.seek(0)
        stdout_text, stderr_text = fo.read(), fe.read()

    if cancelled:
        return SandboxResult(
            True,
            "⏹ 已按用户请求中断执行，容器已清理。\n"
            f"（中断前捕获到的输出如下，可能不完整）\n{stdout_text[-1000:]}",
        )
    if timed_out:
        # 注意：这里必须说明容器已被强制删除，否则用户会以为进程没了
        return SandboxResult(
            True,
            f"❌ 执行超时（>{timeout} 秒），容器已强制终止并清理。\n"
            f"（超时前捕获到的输出如下，可能不完整）\n{stdout_text[-1000:]}",
        )

    return SandboxResult(True, format_exec_output(stdout_text, stderr_text, rc))


def format_exec_output(stdout_text: str, stderr_text: str, rc: int) -> str:
    """把容器输出拼成给模型看的结果。

    单独抽出来是为了**可测**：横幅里的网络模式必须报「实际用的那个」，
    而不是 import 时定死的 ``NETWORK`` 常量。

    实测踩过这个坑：``allow_package_install`` 默认开后容器走的是 ``bridge``，
    但横幅一直写 ``network=none`` —— 用户会以为「我开了联网怎么还是 none」，
    模型也会据此做出错误的环境判断。
    """
    out = ""
    if stdout_text:
        out += f"【stdout】\n{stdout_text}"
    if stderr_text:
        out += f"\n【stderr】\n{stderr_text}"
    out += f"\n\n【退出码】{rc}\n【沙箱】docker:{IMAGE}（network={network_mode()}）"
    return out



def run_argv(
    argv: list[str],
    timeout: int,
    project_root: str,
    *,
    allow_host_fallback: bool = False,
    cancel_event=None,
) -> SandboxResult:
    """把一个命令（argv 列表）安全地转成 shell 脚本后送进沙箱。

    每个参数都用 shlex.quote 转义，因此 argv 里的引号/分号不会被 shell 解释，
    仍然保持「参数列表」的安全性。
    """
    script = " ".join(shlex.quote(a) for a in argv)
    return run_script(
        script,
        timeout,
        project_root,
        allow_host_fallback=allow_host_fallback,
        cancel_event=cancel_event,
    )


HOST_EXEC_NOTICE = "⚠️ 未启用 Docker 沙箱，以下命令直接在宿主机执行（风险自负）\n"
