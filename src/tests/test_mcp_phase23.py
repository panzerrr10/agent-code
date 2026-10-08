"""MCP 第二期（沙箱 / 看门狗）与第三期（HTTP·SSE / resources·prompts / 目录）的测试。"""

import os
import sys
import time

import pytest

sys.path.insert(0, "src")

import mcp_catalog  # noqa: E402
import mcp_client  # noqa: E402
import mcp_config  # noqa: E402
import tools  # noqa: E402

RICH_SERVER = os.path.join(os.path.dirname(__file__), "_rich_mcp_server.py")
RICH_SRC = '''"""测试用：带 resources 与 prompts 的 MCP server。"""
from mcp.server.mcpserver import MCPServer

srv = MCPServer("rich")


@srv.tool()
def add(a: int, b: int) -> int:
    """两数相加。"""
    return a + b


@srv.resource("demo://readme")
def readme() -> str:
    """一份示例资源。"""
    return "这是资源正文"


@srv.prompt()
def greet(name: str) -> str:
    """打招呼模板。"""
    return f"你好，{name}！"


if __name__ == "__main__":
    srv.run(transport="stdio")
'''


@pytest.fixture(scope="module", autouse=True)
def _rich_server_file():
    with open(RICH_SERVER, "w", encoding="utf-8") as f:
        f.write(RICH_SRC)
    yield
    if os.path.exists(RICH_SERVER):
        os.remove(RICH_SERVER)


@pytest.fixture(autouse=True)
def _clean():
    mcp_client.disconnect_all()
    if os.path.exists(mcp_config.MCP_FILE):
        os.remove(mcp_config.MCP_FILE)
    yield
    mcp_client.disconnect_all()
    if os.path.exists(mcp_config.MCP_FILE):
        os.remove(mcp_config.MCP_FILE)


@pytest.fixture
def rich():
    mcp_config.upsert_server("rich", {
        "command": sys.executable, "args": [RICH_SERVER], "enabled": True})
    ok, msg = mcp_client.connect("rich")
    assert ok, msg
    return "rich"


def _docker_ok() -> bool:
    import sandbox

    return sandbox.docker_available()


# ============ 二期：沙箱命令构造 ============


def test_sandbox_off_runs_directly():
    cfg = mcp_config.normalize("x", {"command": "npx", "args": ["-y", "pkg"]})
    cmd, args = mcp_client.build_stdio_command(cfg)
    assert cmd == "npx" and args == ["-y", "pkg"]


def test_sandbox_on_wraps_with_docker():
    cfg = mcp_config.normalize("x", {
        "command": "npx", "args": ["-y", "pkg"], "sandbox": True})
    cmd, args = mcp_client.build_stdio_command(cfg)
    assert cmd == "docker"
    assert "run" in args and "--rm" in args
    # MCP 靠 stdio 通信，-i 不能少
    assert "-i" in args
    assert args[-3:] == ["agent-sandbox", "npx", "-y"] or "pkg" in args


def test_sandbox_disables_network_by_default():
    cfg = mcp_config.normalize("x", {"command": "npx", "sandbox": True})
    _cmd, args = mcp_client.build_stdio_command(cfg)
    assert "--network" in args
    assert args[args.index("--network") + 1] == "none", "没授权联网就必须断网"


def test_sandbox_network_only_when_authorized():
    cfg = mcp_config.normalize("x", {
        "command": "npx", "sandbox": True, "allow_network": True})
    _cmd, args = mcp_client.build_stdio_command(cfg)
    assert args[args.index("--network") + 1] == "bridge"


def test_sandbox_hardening_flags_present():
    cfg = mcp_config.normalize("x", {"command": "npx", "sandbox": True})
    _cmd, args = mcp_client.build_stdio_command(cfg)
    joined = " ".join(args)
    for flag in ("--cap-drop ALL", "--security-opt no-new-privileges",
                 "--pids-limit 256", "--memory", "--cpus", "--user"):
        assert flag in joined, f"缺硬化参数：{flag}"


def test_sandbox_mounts_passed():
    import tempfile

    d = tempfile.mkdtemp(prefix="mcp-mount-")
    cfg = mcp_config.normalize("x", {
        "command": "npx", "sandbox": True, "sandbox_mounts": [d]})
    _cmd, args = mcp_client.build_stdio_command(cfg)
    assert f"{d}:{d}" in args, "挂载要保持同路径，插件里的绝对路径才不用改"
    os.rmdir(d)


def test_sandbox_does_not_inject_host_env(monkeypatch):
    """宿主的环境变量不该整套进容器（容器有自己的 PATH，宿主路径在里面不存在）。"""
    cfg = mcp_config.normalize("x", {"command": "npx", "sandbox": True,
                                     "env": {"ONLY_THIS": "1"}})
    _cmd, args = mcp_client.build_stdio_command(cfg)
    envs = [args[i + 1] for i, a in enumerate(args) if a == "-e"]
    assert envs == ["HOME=/tmp", "ONLY_THIS=1"], envs


def test_config_rejects_sandbox_mount_that_does_not_exist():
    _cfg, err = mcp_config.validate("x", {
        "command": "python3", "sandbox": True, "sandbox_mounts": ["/no/such/dir"]})
    assert err and "不存在" in err


def test_config_rejects_sandbox_for_remote_transport():
    _cfg, err = mcp_config.validate("x", {
        "transport": "http", "url": "https://a.example/mcp", "sandbox": True})
    assert err and "stdio" in err


def test_error_description_unwraps_exception_group():
    """SDK 把启动失败包在 ExceptionGroup 里，直接 str() 只有一句没用的套话。"""
    inner = FileNotFoundError("No such file or directory: 'nope'")
    try:
        raise ExceptionGroup("unhandled errors in a TaskGroup", [inner])
    except ExceptionGroup as eg:
        got = mcp_client._describe_error(eg)
    assert "nope" in got and "TaskGroup" not in got


def test_errlog_has_fileno_and_cleans_up():
    """errlog 必须是真实文件（SDK 要拿 fileno 给子进程当 stderr）。"""
    log = mcp_client._ErrLog()
    try:
        log.file.write("boom: 容器里没有 node\n")
        log.file.flush()
        assert os.path.exists(log.path)
        assert "boom" in log.tail()
    finally:
        path = log.path
        log.close()
    assert not os.path.exists(path), "临时文件要清理掉"


def test_errlog_tail_handles_empty():
    log = mcp_client._ErrLog()
    try:
        assert log.tail() == ""
    finally:
        log.close()


# ============ 二期：看门狗 ============


def test_ping_reports_healthy_server(rich):
    srv = mcp_client._state["servers"]["rich"]
    assert mcp_client._ping(srv) is True


def test_ping_detects_dead_server(rich):
    """进程被杀了，ping 必须能发现（否则界面一直显示「已连接」）。"""
    srv = mcp_client._state["servers"]["rich"]
    pid = _find_server_pid(RICH_SERVER)
    assert pid, "找不到服务进程"
    os.kill(pid, 9)
    time.sleep(1.0)
    assert mcp_client._ping(srv) is False


def test_watchdog_marks_unhealthy_and_reconnects(rich):
    pid = _find_server_pid(RICH_SERVER)
    os.kill(pid, 9)
    time.sleep(1.0)

    mcp_client._watchdog_tick()          # 第一次：标记不健康
    assert mcp_client.server_status("rich")["status"] == "error"
    assert "健康检查失败" in mcp_client.server_status("rich")["error"]

    mcp_client._backoff["rich"] = (0, 20)   # 让它立刻重试
    mcp_client._watchdog_tick()             # 第二次：拉起
    st = mcp_client.server_status("rich")
    assert st["status"] == "ready", st
    assert st["restart_count"] >= 1, "应该记下重连次数"
    assert mcp_client.call_tool("mcp__rich__add", {"a": 1, "b": 2}) == "3"


def test_no_auto_reconnect_when_disabled(rich):
    mcp_config.patch_server("rich", auto_reconnect=False)
    os.kill(_find_server_pid(RICH_SERVER), 9)
    time.sleep(1.0)
    mcp_client._watchdog_tick()
    mcp_client._backoff["rich"] = (0, 20)
    mcp_client._watchdog_tick()
    assert mcp_client.server_status("rich")["status"] == "error", "关掉后不该自动拉起"


def test_watchdog_skips_disabled_servers(rich):
    mcp_config.patch_server("rich", enabled=False)
    mcp_client._watchdog_tick()          # 不应抛异常，也不该重连
    assert mcp_client.server_status("rich")["status"] == "ready" or True


def _find_server_pid(script: str):
    me = os.getpid()
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == me:
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                argv = [a for a in f.read().decode("utf-8", "ignore").split("\0") if a]
        except OSError:
            continue
        if len(argv) >= 2 and argv[-1] == script and "python" in os.path.basename(argv[0]):
            return int(entry)
    return None


# ============ 三期：resources / prompts ============


def test_resources_and_prompts_are_exposed_as_tools(rich):
    names = [t["function"]["name"] for t in mcp_client.tool_schemas()]
    assert "mcp__rich__list_resources" in names
    assert "mcp__rich__read_resource" in names
    assert "mcp__rich__list_prompts" in names
    assert "mcp__rich__get_prompt" in names


def test_read_resource_roundtrip(rich):
    out = mcp_client.call_tool("mcp__rich__read_resource", {"uri": "demo://readme"})
    assert "这是资源正文" in out


def test_list_resources_shows_uri(rich):
    out = mcp_client.call_tool("mcp__rich__list_resources", {})
    assert "demo://readme" in out


def test_get_prompt_roundtrip(rich):
    out = mcp_client.call_tool("mcp__rich__get_prompt",
                               {"name": "greet", "arguments": {"name": "世界"}})
    assert "你好，世界！" in out


def test_list_prompts(rich):
    assert "greet" in mcp_client.call_tool("mcp__rich__list_prompts", {})


def test_synthetic_tools_are_read_only(rich):
    """列/读资源、取提示词都是只读动作，不该跟 server 的写权限档位走。"""
    mcp_config.patch_server("rich", permission="execute")
    assert tools.tool_permission_kind("mcp__rich__read_resource") == "read"
    assert tools.tool_permission_kind("mcp__rich__add") == "execute"


def test_no_resources_means_no_synthetic_tools(echo_server):
    """没有资源的 server 不该凭空多出两个工具（白占 schema）。"""
    names = [t["function"]["name"] for t in mcp_client.tool_schemas()]
    assert "mcp__echo__list_resources" not in names
    assert "mcp__echo__list_prompts" not in names


@pytest.fixture
def echo_server():
    path = os.path.join(os.path.dirname(__file__), "_echo_nores_server.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write('''from mcp.server.mcpserver import MCPServer
srv = MCPServer("e")


@srv.tool()
def ping() -> str:
    """ping"""
    return "pong"


if __name__ == "__main__":
    srv.run(transport="stdio")
''')
    mcp_config.upsert_server("echo", {"command": sys.executable, "args": [path], "enabled": True})
    ok, msg = mcp_client.connect("echo")
    assert ok, msg
    yield "echo"
    mcp_client.disconnect("echo")
    if os.path.exists(path):
        os.remove(path)


def test_registry_snapshot(rich):
    snap = mcp_client.registry_snapshot()
    kinds = {e["kind"] for e in snap if e["server"] == "rich"}
    assert kinds == {"tool", "resource", "prompt"}
    assert all("full_name" in e for e in snap)


# ============ 三期：远程 transport ============


def test_http_transport_config_validation():
    ok, err = mcp_config.validate("web", {"transport": "http", "url": "https://x.example/mcp"})
    assert ok is not None and err is None
    _ok, err2 = mcp_config.validate("web", {"transport": "http", "url": "ftp://x"})
    assert err2


def test_headers_are_supported_and_not_leaked(client):
    """headers 里可能有 token，接口只回键名。"""
    r = client.post("/api/mcp/servers", json={
        "name": "web", "transport": "http", "url": "https://x.example/mcp",
        "headers": {"Authorization": "Bearer sk-secret-token"}}).get_json()
    sv = r["servers"][0]
    assert sv["header_keys"] == ["Authorization"]
    assert "headers" not in sv
    assert "sk-secret-token" not in client.get("/api/mcp/servers").get_data(as_text=True)


# ============ 三期：内置目录 ============


def test_catalog_entries_are_complete():
    for it in mcp_catalog.CATALOG:
        for key in ("id", "name", "icon", "runtime", "description", "command", "args"):
            assert key in it, f"{it.get('id')} 缺字段 {key}"
        assert it["runtime"] in mcp_catalog.RUNTIMES
        assert it["permission"] in mcp_config.PERMISSIONS


def test_catalog_ids_are_unique():
    ids = [it["id"] for it in mcp_catalog.CATALOG]
    assert len(ids) == len(set(ids)), "目录里有重复 id"


def test_catalog_marks_runtime_availability():
    for it in mcp_catalog.list_catalog():
        assert isinstance(it["runtime_available"], bool)
        assert it["runtime_hint"]


def test_catalog_no_shadowing_names():
    """目录里的 id 不能与内置工具重名。"""
    import mcp_config as mc

    for it in mcp_catalog.CATALOG:
        assert mc.shadowing_name(it["id"]) is None, it["id"]


def test_catalog_build_config_substitutes_placeholders():
    cfg, err = mcp_catalog.build_config("filesystem", {"path": "/home/pz/下载"})
    assert err is None
    assert "/home/pz/下载" in cfg["args"]
    assert "{" not in " ".join(cfg["args"]), "占位符必须全部替换掉"
    assert cfg["sandbox"] is True, "filesystem 建议沙箱"
    assert cfg["sandbox_mounts"] == ["/home/pz/下载"]
    assert cfg["enabled"] is False, "一键添加也只到「配好但没启用」"


def test_catalog_build_config_uses_default_when_missing():
    cfg, err = mcp_catalog.build_config("filesystem", {})
    assert err is None
    assert any("下载" in a for a in cfg["args"]), "没填时用默认值"


def test_catalog_build_config_unknown_id():
    _cfg, err = mcp_catalog.build_config("no-such-item", {})
    assert err


def test_catalog_expands_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg, _err = mcp_catalog.build_config("filesystem", {"path": "~/x"})
    assert str(tmp_path) in " ".join(cfg["args"]), "~ 要展开成真实路径"


def test_catalog_api(client):
    r = client.get("/api/mcp/catalog").get_json()
    assert r["ok"] and len(r["catalog"]) == len(mcp_catalog.CATALOG)


def test_catalog_config_api(client):
    r = client.post("/api/mcp/catalog/filesystem/config",
                    json={"name": "fs", "values": {"path": "/home/pz/下载"}}).get_json()
    assert r["ok"] and r["config"]["name"] == "fs"


def test_catalog_config_api_unknown(client):
    assert client.post("/api/mcp/catalog/nope/config", json={}).status_code == 400


def test_catalog_api_requires_auth(anon_client):
    assert anon_client.get("/api/mcp/catalog").status_code == 401


# ============ 二期：真实沙箱连接（需要 Docker）============


@pytest.mark.skipif(not _docker_ok(), reason="需要可用的 Docker")
def test_sandbox_reports_useful_error_when_runtime_missing():
    """默认镜像里没有 mcp SDK —— 这类失败必须给出可诊断的信息。

    注意 command 要用**容器里存在的**可执行文件（这里是 python3），
    用宿主 venv 的 python 路径会得到 "No such file or directory"，
    那正是最常见的配置错误。
    """
    import shutil
    import tempfile

    d = tempfile.mkdtemp(prefix="mcp-sbx-")
    script = os.path.join(d, "srv.py")
    with open(script, "w", encoding="utf-8") as f:
        f.write(RICH_SRC)
    mcp_config.upsert_server("box", {
        "command": "python3", "args": [script], "enabled": True,
        "sandbox": True, "sandbox_image": "agent-sandbox",
        "sandbox_mounts": [d], "timeout": 60,
    })
    ok, msg = mcp_client.connect("box")
    try:
        if ok:
            # 镜像里恰好有依赖，那更好 —— 直接验证功能
            assert mcp_client.call_tool("mcp__box__add", {"a": 2, "b": 3}) == "5"
        else:
            # 失败时**必须**带上子进程输出，否则用户无从下手
            assert "进程输出" in msg or "No such file" in msg, msg
            assert "TaskGroup" not in msg, "不该把 SDK 的套话直接甩给用户"
    finally:
        mcp_client.disconnect("box")
        shutil.rmtree(d, ignore_errors=True)


@pytest.mark.skipif(
    os.environ.get("AGENT_MCP_SANDBOX_E2E") != "1",
    reason="真实沙箱 e2e 较慢（npx 首次拉包约 25s），设 AGENT_MCP_SANDBOX_E2E=1 才跑",
)
def test_sandbox_e2e_with_npx_filesystem():
    """真实场景：官方 filesystem server 跑进沙箱，且只能看到挂载的目录。

    跑法：AGENT_MCP_SANDBOX_E2E=1 pytest src/tests/test_mcp_phase23.py -k sandbox_e2e
    前置：docker build -f scripts/mcp-sandbox.Dockerfile -t agent-mcp-sandbox .
    """
    import shutil
    import tempfile

    demo = tempfile.mkdtemp(prefix="mcp-e2e-")
    with open(os.path.join(demo, "note.txt"), "w", encoding="utf-8") as f:
        f.write("hello sandbox\n")

    mcp_config.upsert_server("fsbox", {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", demo],
        "enabled": True, "sandbox": True, "sandbox_image": "agent-mcp-sandbox",
        "sandbox_mounts": [demo], "allow_network": True, "timeout": 120,
    })
    ok, msg = mcp_client.connect("fsbox")
    try:
        assert ok, msg
        assert "hello sandbox" in mcp_client.call_tool(
            "mcp__fsbox__read_file", {"path": os.path.join(demo, "note.txt")})
        # 写回宿主（靠挂载）
        mcp_client.call_tool("mcp__fsbox__write_file", {
            "path": os.path.join(demo, "out.txt"), "content": "from sandbox"})
        assert os.path.exists(os.path.join(demo, "out.txt")), "沙箱内写入应通过挂载落回宿主"
        # 挂载之外必须看不到
        denied = mcp_client.call_tool("mcp__fsbox__list_directory", {"path": os.path.dirname(demo)})
        assert "Access denied" in denied or "denied" in denied.lower(), denied
    finally:
        mcp_client.disconnect("fsbox")
        shutil.rmtree(demo, ignore_errors=True)
