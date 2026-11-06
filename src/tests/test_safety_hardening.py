"""本次安全加固的回归测试。

对应改动前的**实测漏洞**：

1. ``rm -r`` / ``rm -R`` 完全放行（黑名单只写了 ``rm -rf`` 等几种字面拼写）
2. ``find . -delete`` 既不被黑名单拦，又被 ``is_safe_command`` 判为「只读安全」
   → 等级 2 下**零确认递归删除**
3. ``cat .ssh/id_ed25519`` / ``cat .aws/credentials`` / ``cat .kube/config`` 放行
   （黑名单只写了字面量 ``~/.ssh`` 和 ``id_rsa``）
4. ``printenv`` / ``cat .env`` 被判为「安全」→ 零确认拿到密钥
5. ``run_python`` 完全不经过拦截层
6. Agent 的 read_file 能读出 ``data/providers.json`` 里的明文 API Key
"""

import os

import pytest

import policy
import tools
from paths import is_allowed_path, is_secret_path

HOME = os.path.expanduser("~")


@pytest.fixture(autouse=True)
def _default_policy(monkeypatch, tmp_path):
    """这些用例必须跑在**默认策略**下（外发全关），否则断言会互相干扰。"""
    monkeypatch.setattr(policy, "SETTINGS_FILE", str(tmp_path / "policy.json"))
    for env_name in policy.FLAGS.values():
        monkeypatch.delenv(env_name, raising=False)
    policy.reset_cache()
    yield
    policy.reset_cache()


# ============ 1. 递归删除的各种拼写 ============


@pytest.mark.parametrize(
    "cmd",
    [
        "rm -r /tmp/x",
        "rm -R /tmp/x",
        "rm -rf /tmp/x",
        "rm -fr /tmp/x",
        "rm -r -f /tmp/x",
        "rm -f -r /tmp/x",
        "rm --recursive /tmp/x",
        "rm --force /tmp/x",
        "/bin/rm -r /tmp/x",
        "sudo rm -r /tmp/x",
        # 引号拆词绕过（归一化后还原成 rm -rf）
        "r''m -rf /tmp/x",
        'r"m" -rf /tmp/x',
    ],
)
def test_recursive_rm_is_blocked(cmd):
    assert tools.check_banned(cmd) is not None, f"必须拦：{cmd}"


def test_plain_rm_without_flags_is_not_hard_blocked():
    """不带标志的 rm 不硬拦 —— 它走「需要确认」那一档（rm 不在只读前缀里）。"""
    assert tools.check_banned("rm a.txt") is None
    assert tools.is_safe_command("rm a.txt") is False


# ============ 2. find 的遍历删除 / 执行 ============


@pytest.mark.parametrize(
    "cmd",
    [
        "find . -delete",
        "find /home/pz -name '*.py' -delete",
        "find . -exec rm -r {} +",
        "find . -execdir sh -c 'x' ;",
        "find . -ok rm {} ;",
    ],
)
def test_destructive_find_is_blocked(cmd):
    assert tools.check_banned(cmd) is not None, f"必须拦：{cmd}"


def test_safe_find_is_allowed_and_not_confirm_gated():
    """普通 find 仍然可用且不需要确认 —— 开发者天天用，不能一刀切。"""
    assert tools.check_banned('find . -name "*.java"') is None
    assert tools.is_safe_command("find . -name x") is True


def test_find_with_delete_is_never_safe():
    """双保险：即使黑名单被绕过，is_safe_command 也不能把它当只读。"""
    assert tools.is_safe_command("find . -delete") is False
    assert tools.is_safe_command("find . -exec rm {} ;") is False


# ============ 3. 凭据文件的读取 ============


@pytest.mark.parametrize(
    "cmd",
    [
        "cat .ssh/id_ed25519",
        "cat $HOME/.ssh/id_ed25519",
        f"cat {HOME}/.ssh/id_rsa",
        "cat ~/.ssh/id_rsa",
        "cat .aws/credentials",
        "cat .kube/config",
        "cat .docker/config.json",
        "cat ~/.gnupg/secring.gpg",
        "cat .git-credentials",
        "cat .netrc",
        "head -5 .ssh/id_ed25519",
        "cat data/providers.json",
        "cat .agent-code/auth_token",
    ],
)
def test_credential_reads_are_blocked(cmd):
    assert tools.check_banned(cmd) is not None, f"必须拦：{cmd}"


# ============ 4. 会打印密钥的命令不再是「安全命令」 ============


@pytest.mark.parametrize("cmd", ["printenv", "env", "cat .env", "cat .npmrc", "cat .pypirc"])
def test_secret_printing_commands_need_confirmation(cmd):
    """等级 2 下必须弹确认，不能零确认执行。"""
    assert tools.is_safe_command(cmd) is False, f"不能判为安全：{cmd}"


def test_normal_read_commands_still_safe():
    for cmd in ["ls -la", "cat README.md", "pwd", "git status", "grep -rn foo .", "wc -l a.py"]:
        assert tools.is_safe_command(cmd) is True, f"不该误伤：{cmd}"


# ============ 5. run_python 也过拦截层 ============


def test_run_python_blocks_credential_paths():
    """run_python 原来完全不调 check_banned，沙箱里可以直接读私钥。"""
    out = tools.run_python("print(open('/workspace/.ssh/id_ed25519').read())")
    assert "拒绝" in out
    assert "id_ed25519" in out or "SSH" in out


def test_run_python_blocks_destructive_rm():
    out = tools.run_python("import subprocess; subprocess.run('rm -rf /tmp/x', shell=True)")
    assert "拒绝" in out


def test_run_python_still_runs_normal_code(monkeypatch):
    """正常代码不能被误拦（拦截只针对路径/命令字面量）。"""
    import sandbox

    class _R:
        ran = True
        output = "4"

    monkeypatch.setattr(sandbox, "run_script", lambda *a, **k: _R())
    assert tools.run_python("print(2 + 2)").strip() == "4"


# ============ 6. Agent 工具不得读取应用自身机密 ============


def test_secret_paths_are_rejected_for_agent_tools():
    """这是「密钥只进不出」的补漏：API 层做了掩码，但文件读取是旁路。"""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("providers.json", "auth_token", "policy.json"):
        p = os.path.join(base, "data", name)
        assert is_secret_path(p) is True
        assert is_allowed_path(p) is False, f"Agent 不应能读 {name}"


def test_project_env_files_rejected():
    assert is_allowed_path(os.path.join(HOME, ".env")) is False
    assert is_allowed_path("/tmp/proj/.env") is False
    assert is_allowed_path("/tmp/proj/.env.production") is False


def test_env_example_is_still_readable():
    """.env.example 是模板（不含真密钥），不该被拦。"""
    assert is_secret_path("/tmp/proj/.env.example") is False


def test_normal_project_files_still_allowed():
    """普通源码文件不能被机密名单误伤。

    注意这里用 home 下的路径：``is_allowed_path``（Agent 工具）只放行 home，
    home 之外由用户显式选为工作区后走 ``is_path_permitted``（Web 面板）。
    这是既有设计，本次改动没有动它。
    """
    assert is_allowed_path(os.path.join(HOME, "projects", "main.py")) is True
    assert is_allowed_path(os.path.join(HOME, "projects", "src", "App.java")) is True


def test_is_secret_path_handles_nonexistent():
    assert is_secret_path("/does/not/exist/id_rsa") is True
    assert is_secret_path("/does/not/exist/main.py") is False
