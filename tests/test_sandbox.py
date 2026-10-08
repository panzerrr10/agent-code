"""Docker 沙箱的回归测试。

多数用例**不依赖真实 Docker**（CI 上也能跑）：用 monkeypatch 模拟
「Docker 不可用 / 镜像缺失」，验证回退策略与拒绝策略是否符合预期。

最后三个用例在具备 Docker 与镜像时才运行，做真实挂载、网络隔离与 git 验证。
"""

import os
import shlex
import shutil
import subprocess
import threading
import time

import pytest

import runtime
import sandbox
import tools


@pytest.fixture(autouse=True)
def _clean_state():
    """docker 可用性缓存与运行时状态都不跨用例串味。"""
    sandbox.reset_cache()
    runtime.reset_all()
    runtime.set_session_id(None)
    yield
    sandbox.reset_cache()
    runtime.reset_all()
    runtime.set_session_id(None)


@pytest.fixture(autouse=True)
def _restore_sandbox_flags():
    """恢复被用例改写过的模块级开关。"""
    original = (sandbox.ALLOW_HOST_EXEC, sandbox.TESTS_IN_SANDBOX)
    yield
    sandbox.ALLOW_HOST_EXEC, sandbox.TESTS_IN_SANDBOX = original


# ============ 策略（不需要 Docker）============


def test_refuses_arbitrary_code_when_docker_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "docker_available", lambda: False)
    monkeypatch.setattr(sandbox, "ALLOW_HOST_EXEC", False)

    res = sandbox.run_script("echo pwned", 5, str(tmp_path))
    assert res.ran is True, "不可用时必须由沙箱层给出结论，不能让调用方落到宿主机"
    assert "拒绝执行" in res.output
    assert "AGENT_ALLOW_HOST_EXEC" in res.output


def test_controlled_commands_may_fall_back_to_host(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "docker_available", lambda: False)
    monkeypatch.setattr(sandbox, "ALLOW_HOST_EXEC", False)

    res = sandbox.run_script("git status", 5, str(tmp_path), allow_host_fallback=True)
    assert res.ran is False, "受控命令（git）应允许调用方回退宿主机执行"


def test_explicit_host_exec_optin_allows_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "docker_available", lambda: False)
    monkeypatch.setattr(sandbox, "ALLOW_HOST_EXEC", True)

    res = sandbox.run_script("echo hi", 5, str(tmp_path))
    assert res.ran is False


def test_reports_missing_image(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "docker_available", lambda: True)
    monkeypatch.setattr(sandbox, "image_available", lambda: False)

    res = sandbox.run_script("echo hi", 5, str(tmp_path))
    assert res.ran is True
    assert "找不到沙箱镜像" in res.output
    assert "docker build" in res.output


# ============ 容器参数（不需要 Docker）============


def test_docker_cmd_mounts_project_root_and_hardens(tmp_path):
    cmd = sandbox._build_docker_cmd(str(tmp_path), "echo hi")
    joined = " ".join(cmd)

    assert cmd[0] == "docker" and cmd[1] == "run"
    assert "--rm" in cmd, "每次执行都应起临时容器，避免残留状态"
    # 权限收敛
    assert "--cap-drop" in cmd and "ALL" in cmd
    assert "--security-opt" in cmd and "no-new-privileges" in cmd
    assert "--pids-limit" in cmd
    assert "--user" in cmd and f"{os.getuid()}:{os.getgid()}" in cmd
    # 资源限制
    assert "--memory" in cmd and sandbox.MEMORY in cmd
    assert "--cpus" in cmd and sandbox.CPUS in cmd
    # 挂载与工作目录
    assert f"{tmp_path}:/workspace" in joined
    assert cmd[cmd.index("-w") + 1] == "/workspace"
    # 网络由配置决定（默认 none）
    assert cmd[cmd.index("--network") + 1] == sandbox.NETWORK


def test_run_argv_quotes_every_argument(monkeypatch, tmp_path):
    captured = {}

    def fake_run_script(script, timeout, root, allow_host_fallback=False, cancel_event=None):
        captured["script"] = script
        return sandbox.SandboxResult(True, "ok")

    monkeypatch.setattr(sandbox, "run_script", fake_run_script)

    payload = 'x"; rm -rf /; echo "'
    sandbox.run_argv(["git", "commit", "-m", payload], 5, str(tmp_path))

    script = captured["script"]
    # 危险参数必须整体被 shlex.quote 包住，不能裸奔到 shell
    assert shlex.quote(payload) in script
    assert script.startswith("git commit -m ")


# ============ 工具层接入（不需要 Docker）============


def test_run_python_refuses_and_does_not_touch_host(monkeypatch, tmp_path):
    marker = tmp_path / "should-not-exist.txt"
    monkeypatch.setattr(sandbox, "docker_available", lambda: False)
    monkeypatch.setattr(sandbox, "ALLOW_HOST_EXEC", False)
    runtime.set_session_id("sbx")
    runtime.set_project_root(str(tmp_path))

    out = tools.run_python(f"open({str(marker)!r}, 'w').write('pwned')")
    assert "拒绝执行" in out
    assert not marker.exists(), "Docker 不可用时竟然在宿主机执行了代码"


def test_run_command_refuses_when_docker_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "docker_available", lambda: False)
    monkeypatch.setattr(sandbox, "ALLOW_HOST_EXEC", False)
    runtime.set_session_id("sbx")
    runtime.set_project_root(str(tmp_path))

    assert "拒绝执行" in tools.run_command("echo hi")


@pytest.mark.skipif(not shutil.which("git"), reason="需要 git")
def test_git_falls_back_to_host_with_explicit_notice(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "docker_available", lambda: False)
    monkeypatch.setattr(sandbox, "ALLOW_HOST_EXEC", False)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    runtime.set_session_id("sbx")
    runtime.set_project_root(str(tmp_path))

    out = tools.git_status()
    # 回退了要明确标注，不能让用户以为还在隔离环境里
    assert "未启用 Docker 沙箱" in out
    assert "退出码" in out


# ============ 真实 Docker（本机具备条件时才跑）============


def _real_docker_ready() -> bool:
    return sandbox.docker_available() and sandbox.image_available()


@pytest.mark.skipif(not _real_docker_ready(), reason="需要 Docker 与沙箱镜像")
def test_real_sandbox_sees_and_writes_the_same_files(tmp_path):
    """沙箱挂载的必须是真实项目根目录：写进去的文件宿主机应立刻可见。"""
    (tmp_path / "host.txt").write_text("from-host", encoding="utf-8")

    res = sandbox.run_script(
        "cat /workspace/host.txt && echo from-sandbox > /workspace/made.txt",
        60,
        str(tmp_path),
    )
    assert res.ran is True
    assert "from-host" in res.output
    assert "【退出码】0" in res.output
    assert (tmp_path / "made.txt").read_text(encoding="utf-8").strip() == "from-sandbox"


@pytest.mark.skipif(not _real_docker_ready(), reason="需要 Docker 与沙箱镜像")
def test_real_sandbox_isolates_network(tmp_path):
    """默认 --network=none：容器内不应能访问外网。"""
    res = sandbox.run_script(
        "python3 -c \"import urllib.request;urllib.request.urlopen('http://example.com',timeout=4)\"",
        60,
        str(tmp_path),
    )
    assert res.ran is True
    assert "【退出码】0" not in res.output, "沙箱内竟然能访问外网"


@pytest.mark.skipif(not _real_docker_ready(), reason="需要 Docker 与沙箱镜像")
def test_real_sandbox_runs_git(tmp_path):
    res = sandbox.run_script("git --version", 60, str(tmp_path))
    assert res.ran is True
    assert "git version" in res.output


# ============ 工具级取消 + 容器清理 ============


def _leftover_exec_containers():
    """本模块起的临时容器名前缀是 agent-exec-，返回残留列表。"""
    r = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True
    )
    return [n for n in r.stdout.split() if n.startswith("agent-exec-")]


@pytest.mark.skipif(not shutil.which("sleep"), reason="需要 sleep 命令")
def test_host_subprocess_is_cancelled_promptly(tmp_path):
    """宿主机子进程（run_tests / git 回退路径）应能被打断，而不是干等超时。"""
    ev = threading.Event()
    threading.Timer(0.5, ev.set).start()

    t0 = time.time()
    out = tools._run_subprocess(["sleep", "30"], str(tmp_path), 60, cancel_event=ev)
    elapsed = time.time() - t0

    assert "中断" in out
    assert elapsed < 10, f"取消没有及时生效，耗时 {elapsed:.1f}s"


@pytest.mark.skipif(not shutil.which("sleep"), reason="需要 sleep 命令")
def test_host_subprocess_still_times_out(tmp_path):
    t0 = time.time()
    out = tools._run_subprocess(["sleep", "30"], str(tmp_path), 2)
    elapsed = time.time() - t0
    assert "超时" in out
    assert elapsed < 10, f"超时没有生效，耗时 {elapsed:.1f}s"


@pytest.mark.skipif(not _real_docker_ready(), reason="需要 Docker 与沙箱镜像")
@pytest.mark.skipif(not shutil.which("sleep"), reason="需要 sleep 命令")
def test_real_sandbox_cancel_kills_container(tmp_path):
    """点停止后：容器必须被真正删除，而不是只 kill 掉 docker 客户端。"""
    ev = threading.Event()
    threading.Timer(1.0, ev.set).start()

    t0 = time.time()
    res = sandbox.run_script("sleep 60", 120, str(tmp_path), cancel_event=ev)
    elapsed = time.time() - t0

    assert res.ran is True
    assert "中断" in res.output
    assert elapsed < 15, f"中断没有及时生效，耗时 {elapsed:.1f}s"
    assert _leftover_exec_containers() == [], "取消后容器仍残留在 docker ps 里"


@pytest.mark.skipif(not _real_docker_ready(), reason="需要 Docker 与沙箱镜像")
@pytest.mark.skipif(not shutil.which("sleep"), reason="需要 sleep 命令")
def test_real_sandbox_timeout_leaves_no_container(tmp_path):
    """超时同样要清理容器 —— 修复前会一直残留占资源，且提示文案还谎称已终止。"""
    res = sandbox.run_script("sleep 60", 3, str(tmp_path))
    assert res.ran is True
    assert "超时" in res.output
    assert "已强制终止并清理" in res.output, "提示应说明容器已被强制清理"
    assert _leftover_exec_containers() == [], "超时后容器仍残留在 docker ps 里"
