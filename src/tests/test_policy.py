"""执行策略开关（联网 / 安装依赖 / 推送 / 评审跑测试）。

这些开关的存在本身就是本次改动的核心之一：改动前 `pip install`、`git pull`
被**硬编码**在 BANNED_RULES 里一律拦死，对程序员工具是致命的（装不了依赖、
拉不到代码）。现在拆成显式开关，**默认仍然全关**，行为不变。
"""

import json

import pytest

import policy


@pytest.fixture(autouse=True)
def _clean_policy(tmp_path, monkeypatch):
    """每个用例用独立的 policy.json，并且清掉可能存在的 *_ALLOW_* 环境变量。"""
    monkeypatch.setattr(policy, "SETTINGS_FILE", str(tmp_path / "policy.json"))
    for env_name in policy.FLAGS.values():
        monkeypatch.delenv(env_name, raising=False)
    policy.reset_cache()
    yield
    policy.reset_cache()


# ============ 默认值：安全默认不变 ============


def test_network_and_push_default_off():
    """联网与推送默认必须关 —— 这两项会把数据发出去。"""
    p = policy.get_policy()
    assert p["allow_network"] is False
    assert p["allow_git_push"] is False


def test_package_install_defaults_on():
    """装依赖**默认开**（按用户要求）：不能装依赖的编程 Agent 不可用。

    ⚠️ 连带后果：sandbox_network() 会因为此项而默认返回 bridge，
    即「沙箱默认有网」。见 test_sandbox_network_bridge_when_install_on。
    """
    assert policy.get_policy()["allow_package_install"] is True
    import tools

    assert tools.check_banned("pip install requests") is None
    assert tools.check_banned("npm install express") is None
    # 但联网与推送仍然关着
    assert tools.check_banned("curl http://x") is not None
    assert tools.check_banned("git push") is not None


def test_reviewer_tests_default_on():
    """评审跑测试是本次新增能力，默认开（可用开关或等级 1 关掉）。"""
    assert policy.get_policy()["allow_reviewer_tests"] is True


def test_missing_file_falls_back_to_defaults():
    assert policy.get_policy() == policy.defaults()


# ============ 持久化 ============


def test_set_and_reload_roundtrip():
    policy.set_policy({"allow_network": True})
    assert policy.allow_network() is True

    # 模拟「另一个进程」：清缓存后从文件重新读
    policy.reset_cache()
    assert policy.get_policy()["allow_network"] is True

    with open(policy.SETTINGS_FILE, encoding="utf-8") as f:
        assert json.load(f)["allow_network"] is True


def test_set_only_accepts_known_flags():
    """前端塞垃圾字段不应污染配置，也不该报错。"""
    policy.set_policy({"allow_network": True, "evil_key": True})
    policy.reset_cache()
    saved = json.load(open(policy.SETTINGS_FILE, encoding="utf-8"))
    assert "evil_key" not in saved
    assert saved["allow_network"] is True


def test_set_policy_rejects_non_dict():
    with pytest.raises(ValueError):
        policy.set_policy(["not", "a", "dict"])


def test_is_enabled_rejects_unknown_flag():
    with pytest.raises(KeyError):
        policy.is_enabled("nope")


def test_independent_switches():
    """三个开关互相独立 —— 开安装不等于开推送，也不等于开联网。"""
    policy.set_policy({"allow_package_install": True})
    assert policy.allow_package_install() is True
    assert policy.allow_network() is False
    assert policy.allow_git_push() is False


# ============ 环境变量覆盖 ============


def test_env_var_overrides_file(monkeypatch):
    policy.set_policy({"allow_network": True})
    monkeypatch.setenv("AGENT_ALLOW_NETWORK", "0")
    policy.reset_cache()
    assert policy.allow_network() is False, "环境变量优先级必须高于文件"


def test_env_var_enables_without_file(monkeypatch):
    monkeypatch.setenv("AGENT_ALLOW_PACKAGE_INSTALL", "1")
    policy.reset_cache()
    assert policy.allow_package_install() is True


@pytest.mark.parametrize("raw", ["1", "true", "YES", "on", " True "])
def test_truthy_spellings(monkeypatch, raw):
    monkeypatch.setenv("AGENT_ALLOW_NETWORK", raw)
    policy.reset_cache()
    assert policy.allow_network() is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", ""])
def test_falsy_spellings(monkeypatch, raw):
    if raw == "":
        monkeypatch.delenv("AGENT_ALLOW_NETWORK", raising=False)
    else:
        monkeypatch.setenv("AGENT_ALLOW_NETWORK", raw)
    policy.reset_cache()
    assert policy.allow_network() is False


def test_locked_by_env_is_reported(monkeypatch):
    """界面必须知道哪一项被环境变量接管了，否则用户会反复点却没反应。"""
    monkeypatch.setenv("AGENT_ALLOW_NETWORK", "1")
    policy.reset_cache()
    snap = policy.snapshot()
    assert snap["locked_by_env"]["allow_network"] is True
    assert snap["locked_by_env"]["allow_git_push"] is False


# ============ 沙箱网络联动 ============


def test_sandbox_network_bridge_when_install_on(monkeypatch):
    """装依赖默认开 → 沙箱默认 bridge（否则装了也下载不到）。

    想回到「默认禁网」：把 allow_package_install 默认改成 False，
    或显式设 AGENT_SANDBOX_NETWORK=none。
    """
    monkeypatch.delenv("AGENT_SANDBOX_NETWORK", raising=False)
    policy.set_policy({"allow_package_install": True})
    policy.reset_cache()
    assert policy.sandbox_network() == "bridge"


def test_sandbox_network_none_when_everything_off(monkeypatch):
    """三项外发全关时，容器必须回到禁网。"""
    monkeypatch.delenv("AGENT_SANDBOX_NETWORK", raising=False)
    policy.set_policy({"allow_network": False, "allow_package_install": False,
                       "allow_git_push": False})
    policy.reset_cache()
    assert policy.sandbox_network() == "none"


def test_sandbox_network_follows_switches(monkeypatch):
    """开了联网/安装就必须给容器网络，否则「放行了但容器里出不去」。"""
    monkeypatch.delenv("AGENT_SANDBOX_NETWORK", raising=False)
    policy.set_policy({"allow_network": True})
    policy.reset_cache()
    assert policy.sandbox_network() == "bridge"


def test_sandbox_network_explicit_env_wins(monkeypatch):
    monkeypatch.setenv("AGENT_SANDBOX_NETWORK", "host")
    policy.set_policy({"allow_network": True})
    policy.reset_cache()
    assert policy.sandbox_network() == "host"


def test_sandbox_network_mode_used_by_builder(monkeypatch):
    """sandbox 真的会把 network_mode() 用进 docker 参数（不是只改了常量）。"""
    import sandbox

    monkeypatch.delenv("AGENT_SANDBOX_NETWORK", raising=False)
    policy.set_policy({"allow_network": True})
    policy.reset_cache()
    cmd = sandbox._build_docker_cmd("/tmp", "echo hi")
    assert "bridge" in cmd
    assert cmd[cmd.index("--network") + 1] == "bridge"


# ============ 权限：策略必须真的改变命令放行结果 ============


def test_install_can_be_turned_off_again():
    """默认开，但用户必须能关掉它（关掉就回到硬拦）。"""
    import tools

    cmd = "pip install requests"
    assert tools.check_banned(cmd) is None, "默认开"
    policy.set_policy({"allow_package_install": False})
    assert tools.check_banned(cmd) is not None, "关掉后必须硬拦"
    policy.set_policy({"allow_package_install": True})


def test_git_pull_and_push_are_separate_switches():
    import tools

    policy.set_policy({"allow_network": True})
    assert tools.check_banned("git pull") is None
    assert tools.check_banned("git push") is not None, "开联网不等于允许推送"

    policy.set_policy({"allow_git_push": True})
    assert tools.check_banned("git push") is None


def test_policy_does_not_unlock_destructive_commands():
    """开了所有开关，破坏性命令仍然必须被拦。"""
    import tools

    policy.set_policy({
        "allow_network": True,
        "allow_package_install": True,
        "allow_git_push": True,
    })
    assert tools.check_banned("rm -rf /tmp/x") is not None
    assert tools.check_banned("find . -delete") is not None
    assert tools.check_banned("cat ~/.ssh/id_rsa") is not None
    assert tools.check_banned("sudo apt install x") is not None


# ============ HTTP 接口 ============


def test_policy_api_get(client, tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "SETTINGS_FILE", str(tmp_path / "api.json"))
    policy.reset_cache()
    resp = client.get("/api/policy")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["policy"]["allow_network"] is False
    assert "locked_by_env" in body
    assert "sandbox_network" in body


def test_policy_api_post_updates_and_persists(client, tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "SETTINGS_FILE", str(tmp_path / "api.json"))
    policy.reset_cache()
    resp = client.post("/api/policy", json={"allow_package_install": True})
    assert resp.status_code == 200
    assert resp.get_json()["policy"]["allow_package_install"] is True

    policy.reset_cache()          # 模拟另一个进程重读
    assert policy.allow_package_install() is True


def test_policy_api_rejects_bad_payload(client, tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "SETTINGS_FILE", str(tmp_path / "api.json"))
    policy.reset_cache()
    resp = client.post("/api/policy", json=["not", "a", "dict"])
    assert resp.status_code == 400


def test_policy_api_ignores_unknown_fields(client, tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "SETTINGS_FILE", str(tmp_path / "api.json"))
    policy.reset_cache()
    resp = client.post("/api/policy", json={"allow_network": True, "hack": 1})
    assert resp.status_code == 200
    assert "hack" not in resp.get_json()["policy"]


def test_policy_api_requires_auth(anon_client):
    assert anon_client.get("/api/policy").status_code == 401


# ============ 沙箱横幅必须报「实际用的」网络模式 ============


def test_sandbox_banner_reports_actual_network_mode(tmp_path, monkeypatch):
    """实测踩过的坑：横幅原来打印 import 时定死的 NETWORK 常量。

    allow_package_install 默认开后容器走 bridge，横幅却一直写 none ——
    用户会以为「我开了联网怎么还是 none」，**模型也会据此做出错误的环境判断**。
    """
    import sandbox

    monkeypatch.delenv("AGENT_SANDBOX_NETWORK", raising=False)
    policy.set_policy({"allow_network": False, "allow_package_install": False})
    policy.reset_cache()
    assert "network=none" in sandbox.format_exec_output("out", "", 0)

    policy.set_policy({"allow_package_install": True})
    policy.reset_cache()
    banner = sandbox.format_exec_output("out", "", 0)
    assert "network=bridge" in banner
    assert "network=none" not in banner


def test_sandbox_banner_keeps_output_and_exit_code():
    import sandbox

    out = sandbox.format_exec_output("hello", "oops", 127)
    assert "【stdout】\nhello" in out
    assert "【stderr】\noops" in out
    assert "【退出码】127" in out
