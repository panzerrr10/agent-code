"""MCP 插件系统的测试。

重点覆盖三类：
1. **安全默认值** —— 新插件默认不启用、不联网、权限按最保守处理
2. **命名空间** —— 插件不能劫持内置工具名，工具名要符合 OpenAI 格式与长度上限
3. **注入策略** —— 按需注入、总开关、多 Agent 全发模式
"""

import json
import os
import sys
import time

import pytest

sys.path.insert(0, "src")

import db  # noqa: E402
import mcp_client  # noqa: E402
import mcp_config  # noqa: E402
import tools  # noqa: E402

ECHO_SERVER = os.path.join(os.path.dirname(__file__), "_echo_mcp_server.py")


@pytest.fixture(scope="session", autouse=True)
def _echo_server_file():
    """写一个最小 MCP server（不依赖网络与 npm）。"""
    with open(ECHO_SERVER, "w", encoding="utf-8") as f:
        f.write('''"""测试用的最小 MCP server。"""
import os

from mcp.server.mcpserver import MCPServer

srv = MCPServer("echo")


@srv.tool()
def echo(text: str) -> str:
    """把输入原样返回。"""
    return f"ECHO: {text}"


@srv.tool()
def read_secret() -> str:
    """返回环境变量 AGENT_TEST_SECRET，用于验证环境变量白名单。"""
    return "SECRET=" + os.environ.get("AGENT_TEST_SECRET", "<不可见>")


@srv.tool()
def boom() -> str:
    """故意抛异常。"""
    raise ValueError("故意失败")


if __name__ == "__main__":
    srv.run(transport="stdio")
''')
    yield
    if os.path.exists(ECHO_SERVER):
        os.remove(ECHO_SERVER)


@pytest.fixture(autouse=True)
def _clean_mcp():
    """每个用例前后都清空配置与连接，避免全局状态串味。"""
    mcp_client.disconnect_all()
    if os.path.exists(mcp_config.MCP_FILE):
        os.remove(mcp_config.MCP_FILE)
    yield
    mcp_client.disconnect_all()
    if os.path.exists(mcp_config.MCP_FILE):
        os.remove(mcp_config.MCP_FILE)


@pytest.fixture
def echo_server():
    """注册并连接一个真实的 echo server。"""
    mcp_config.upsert_server("echo", {
        "transport": "stdio", "command": sys.executable,
        "args": [ECHO_SERVER], "enabled": True,
    })
    ok, msg = mcp_client.connect("echo")
    assert ok, msg
    return "echo"


# ============ 安全默认值 ============


def test_new_server_defaults_are_safe():
    mcp_config.upsert_server("demo", {"command": "python"})
    cfg = mcp_config.get_server("demo")
    assert cfg["enabled"] is False, "新插件默认必须是关闭的"
    assert cfg["allow_network"] is False, "新插件默认不许联网"
    assert cfg["permission"] == "mutate", "默认要用户确认"


def test_default_permission_is_mutate(client):
    """未知 MCP 工具的权限档位必须是 mutate（要确认），不能悄悄放行。"""
    assert tools.tool_permission_kind("mcp__whatever__thing") == "mutate"
    assert tools.is_privileged_tool("mcp__whatever__thing") is True


def test_server_declared_readonly_hint_is_not_trusted(echo_server):
    """server 自称只读也不算数 —— 档位只由用户标注决定。"""
    # echo server 的工具没有声明 readOnlyHint，且我们没有标注 → mutate
    assert tools.tool_permission_kind("mcp__echo__echo") == "mutate"
    mcp_config.patch_server("echo", permission="read")
    assert tools.tool_permission_kind("mcp__echo__echo") == "read"


# ============ 校验 ============


@pytest.mark.parametrize("name", ["a b", "带中文", "-lead", "", "x" * 33, "a/b"])
def test_rejects_bad_server_name(name):
    _cfg, err = mcp_config.validate(name, {"command": "python"})
    assert err, f"{name!r} 应该被拒绝"


def test_rejects_missing_command():
    _cfg, err = mcp_config.validate("x", {"command": ""})
    assert err and "command" in err


def test_rejects_unknown_executable():
    _cfg, err = mcp_config.validate("x", {"command": "definitely-not-a-real-binary-xyz"})
    assert err and "找不到" in err


def test_rejects_bad_url():
    _cfg, err = mcp_config.validate("x", {"transport": "http", "url": "ftp://nope"})
    assert err


def test_rejects_name_shadowing_builtin_tool():
    """插件名不能和内置工具重名 —— 否则可能劫持看起来安全的工具。"""
    # 命令必须是真实存在的，否则会先撞上「找不到可执行文件」那条错误
    _cfg, err = mcp_config.validate("read_file", {"command": sys.executable})
    assert err and "冲突" in err


# ============ 命名空间 ============


def test_full_name_format():
    assert mcp_client.make_full_name("fs", "read") == "mcp__fs__read"


def test_full_name_sanitises_illegal_chars():
    got = mcp_client.make_full_name("fs", "do/thing?bad")
    assert "/" not in got and "?" not in got
    assert got.startswith("mcp__fs__")


def test_full_name_respects_openai_length_limit():
    got = mcp_client.make_full_name("s" * 32, "t" * 200)
    assert len(got) <= 64, f"OpenAI 函数名上限 64，实际 {len(got)}"


def test_long_tool_names_stay_unique():
    a = mcp_client.make_full_name("s", "x" * 200 + "AAA")
    b = mcp_client.make_full_name("s", "x" * 200 + "BBB")
    assert a != b, "截断后必须仍然唯一"


def test_is_mcp_tool():
    assert mcp_client.is_mcp_tool("mcp__a__b")
    assert not mcp_client.is_mcp_tool("read_file")


# ============ 真实连接与调用 ============


def test_connect_lists_tools(echo_server):
    names = [t["function"]["name"] for t in mcp_client.tool_schemas()]
    assert "mcp__echo__echo" in names
    assert len(names) == 3


def test_call_tool_roundtrip(echo_server):
    assert mcp_client.call_tool("mcp__echo__echo", {"text": "hi"}) == "ECHO: hi"


def test_execute_tool_routes_to_mcp(echo_server):
    """execute_tool 是全项目唯一的派发入口，MCP 工具必须从这里走通。"""
    assert tools.execute_tool("mcp__echo__echo", {"text": "via dispatch"}) == "ECHO: via dispatch"


def test_env_whitelist_does_not_leak_secrets(echo_server, monkeypatch):
    """os.environ 里的密钥不能被传给第三方插件进程。"""
    monkeypatch.setenv("AGENT_TEST_SECRET", "sk-must-not-leak-9999")
    out = mcp_client.call_tool("mcp__echo__read_secret", {})
    assert "must-not-leak" not in out, f"密钥泄漏给插件了：{out}"


def test_tool_error_does_not_crash(echo_server):
    out = mcp_client.call_tool("mcp__echo__boom", {})
    assert isinstance(out, str) and out


def test_unknown_mcp_tool_returns_error():
    assert "未知" in tools.execute_tool("mcp__nope__nope", {})


def test_disconnect_unregisters_tools(echo_server):
    mcp_client.disconnect("echo")
    assert mcp_client.resolve("mcp__echo__echo") is None
    assert "未知" in tools.execute_tool("mcp__echo__echo", {"text": "x"})


def _server_processes() -> list:
    """找出真正在跑测试 server 的进程。

    用 **argv 精确比对**，不用子串匹配：子串匹配会把「命令行里恰好含有该文件名」
    的父进程也算进来 —— 比如 ``bash -c`` 的整段脚本里就带着这个文件名，
    结果明明清理干净了却误报「有残留」。
    """
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                argv = [a for a in f.read().decode("utf-8", "ignore").split("\0") if a]
        except OSError:
            continue
        # 形如：<python> <测试 server 脚本>
        if (len(argv) >= 2 and argv[-1] == ECHO_SERVER
                and "python" in os.path.basename(argv[0])):
            found.append((entry, " ".join(argv)[:110]))
    return found


def test_no_orphan_process_after_disconnect(echo_server):
    """断开后不能留下子进程（这个项目被僵尸进程坑过）。"""
    assert _server_processes(), "连上了却没找到服务进程，说明检测方式有问题"
    mcp_client.disconnect("echo")
    for _ in range(25):
        if not _server_processes():
            break
        time.sleep(0.2)
    assert not _server_processes(), f"断开后仍有残留进程：{_server_processes()}"


# ============ 注入策略 ============


def _mcp_names(task, full=False):
    return [t["function"]["name"] for t in tools.get_tools_schema(task, full=full)
            if t["function"]["name"].startswith("mcp__")]


def test_auto_inject_only_on_keyword(echo_server):
    assert _mcp_names("帮我重构这段代码") == [], "无关任务不应注入（省 token）"
    assert _mcp_names("用 echo 回显一下"), "命中 server 名应注入"


def test_inject_always_and_never(echo_server):
    mcp_config.patch_server("echo", inject="always")
    assert _mcp_names("完全无关的任务")
    mcp_config.patch_server("echo", inject="never")
    assert _mcp_names("用 echo 回显") == []


def test_global_inject_switch(echo_server):
    """设置面板里的总开关。"""
    mcp_config.patch_server("echo", inject="always")
    assert _mcp_names("无关")
    mcp_config.set_inject_enabled(False)
    assert _mcp_names("无关") == [], "总开关关掉后不应注入"
    mcp_config.set_inject_enabled(True)
    assert _mcp_names("无关")


def test_full_mode_also_injects(echo_server):
    """多 Agent 的「全发」模式以前直接 return，导致 MCP 工具永远不可见。"""
    mcp_config.patch_server("echo", inject="always")
    assert _mcp_names("任意", full=True), "full=True 也必须带上 MCP 工具"


def test_disabled_server_not_injected(echo_server):
    mcp_config.patch_server("echo", inject="always", enabled=False)
    assert _mcp_names("任意") == []


# ============ HTTP 接口 ============


def test_api_list_empty(client):
    r = client.get("/api/mcp/servers").get_json()
    assert r["ok"] and r["servers"] == []


def test_api_upsert_safe_defaults(client):
    r = client.post("/api/mcp/servers", json={"name": "demo", "command": sys.executable}).get_json()
    sv = r["servers"][0]
    assert sv["enabled"] is False and sv["allow_network"] is False
    assert sv["permission"] == "mutate"


def test_api_rejects_bad_config(client):
    assert client.post("/api/mcp/servers", json={"name": "a b", "command": "x"}).status_code == 400
    assert client.post("/api/mcp/servers",
                       json={"name": "ok", "command": "no-such-bin-zzz"}).status_code == 400


def test_api_enable_connects_and_lists_tools(client):
    client.post("/api/mcp/servers", json={
        "name": "echo", "command": sys.executable, "args": [ECHO_SERVER]})
    r = client.post("/api/mcp/servers/echo/enabled", json={"enabled": True}).get_json()
    sv = r["servers"][0]
    assert sv["runtime_status"] == "ready"
    assert sv["tool_count"] == 3
    assert "echo" in sv["tools"]


def test_api_disable_disconnects(client):
    client.post("/api/mcp/servers", json={
        "name": "echo", "command": sys.executable, "args": [ECHO_SERVER]})
    client.post("/api/mcp/servers/echo/enabled", json={"enabled": True})
    r = client.post("/api/mcp/servers/echo/enabled", json={"enabled": False}).get_json()
    assert r["servers"][0]["runtime_status"] == "idle"


def test_api_does_not_leak_env_values(client):
    client.post("/api/mcp/servers", json={
        "name": "sec", "command": sys.executable, "args": [ECHO_SERVER],
        "env": {"MY_TOKEN": "sk-secret-value-xyz"}})
    body = client.get("/api/mcp/servers").get_data(as_text=True)
    assert "sk-secret-value-xyz" not in body, "插件 env 的值不能回传前端"
    assert "MY_TOKEN" in body, "键名可以回传（让用户知道配了什么）"


def test_api_inject_switch(client):
    assert client.post("/api/mcp/inject", json={"enabled": False}).get_json()["inject_enabled"] is False
    assert client.post("/api/mcp/inject", json={"enabled": True}).get_json()["inject_enabled"] is True


def test_api_delete_and_404(client):
    client.post("/api/mcp/servers", json={"name": "demo", "command": sys.executable})
    assert client.delete("/api/mcp/servers/demo").status_code == 200
    assert client.delete("/api/mcp/servers/demo").status_code == 404
    assert client.post("/api/mcp/servers/ghost/enabled", json={"enabled": True}).status_code == 404


def test_api_requires_auth(anon_client):
    assert anon_client.get("/api/mcp/servers").status_code == 401
    assert anon_client.post("/api/mcp/servers", json={}).status_code == 401


# ============ 权限闸门集成 ============


def test_permission_gate_applies_to_mcp_tools(client, echo_server):
    """MCP 工具必须走同一道权限闸门。"""
    import agent
    import runtime

    sid = client.post("/sessions", json={}).get_json()["id"]
    runtime.set_session_id(sid)

    mcp_config.patch_server("echo", permission="execute")
    for level, expect in ((1, "deny"), (2, "confirm"), (3, "allow")):
        db.set_permission_level(sid, level)
        action, _ = agent._permission_gate("mcp__echo__echo", {"text": "x"}, False)
        assert action == expect, f"等级 {level} 期望 {expect}，实际 {action}"

    mcp_config.patch_server("echo", permission="read")
    db.set_permission_level(sid, 1)
    action, _ = agent._permission_gate("mcp__echo__echo", {"text": "x"}, False)
    assert action == "allow", "标注为只读的插件在等级 1 下应放行"


def test_confirm_card_is_readable(echo_server):
    """确认卡片不能显示成「mcp__echo__echo {'text': 'hi'}」这种原始样子。"""
    import agent

    ev = agent._confirm_event("mcp__echo__echo", {"text": "你好"}, "tok")
    assert ev["type"] == "confirm_command"
    assert "MCP" in ev["cmd"] and "echo" in ev["cmd"]
    assert "你好" in ev["cmd"]


def test_config_file_not_in_repo_data():
    """测试必须用临时配置，不能碰真实 data/mcp.json。"""
    assert "agentcode-test-" in mcp_config.MCP_FILE, mcp_config.MCP_FILE
    assert json.dumps  # 保持 import 有用
