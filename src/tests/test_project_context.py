"""项目约定（AGENTS.md）的测试。

要点：
1. **只认 AGENTS.md**（用户明确要求不兼容 CLAUDE.md）
2. 多个位置**合并**而不是只取第一个（项目级在前、用户级在后）
3. 必须有大小上限 —— 否则有人写个 1MB 的说明能把上下文吃光
4. 注入方式是**独立的 system 消息**，不拼进 SYSTEM_PROMPT
"""

import os
import sys

import pytest

sys.path.insert(0, "src")

import project_context as pc  # noqa: E402


@pytest.fixture
def home(monkeypatch, tmp_path):
    """把 HOME 指到临时目录，避免读到真实的 ~/.agentcode/AGENTS.md。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(os.path, "expanduser",
                        lambda p: str(tmp_path) if p == "~" else p)
    return tmp_path


@pytest.fixture
def wsdir():
    """在 HOME 下建临时工作区目录。

    **不能用 pytest 的 tmp_path**：它在 /tmp，而路径白名单只放行用户主目录，
    建工作区会被直接拒掉。
    """
    import shutil
    import tempfile

    d = tempfile.mkdtemp(prefix="agentcode-pc-", dir=os.path.expanduser("~"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def ws(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    return str(d)


# ============ 查找与合并 ============


def test_no_file_returns_empty(home, ws):
    r = pc.load(ws)
    assert r["text"] == "" and r["sources"] == []


def test_finds_root_agents_md(home, ws):
    with open(os.path.join(ws, "AGENTS.md"), "w", encoding="utf-8") as f:
        f.write("# 规则\n- 用中文注释\n")
    r = pc.load(ws)
    assert len(r["sources"]) == 1
    assert "用中文注释" in r["text"]


def test_merges_root_and_dotdir(home, ws):
    with open(os.path.join(ws, "AGENTS.md"), "w", encoding="utf-8") as f:
        f.write("根目录规则\n")
    os.makedirs(os.path.join(ws, ".agentcode"), exist_ok=True)
    with open(os.path.join(ws, ".agentcode", "AGENTS.md"), "w", encoding="utf-8") as f:
        f.write("子目录规则\n")
    r = pc.load(ws)
    assert len(r["sources"]) == 2, "找到多个时应合并，而不是只取第一个"
    assert "根目录规则" in r["text"] and "子目录规则" in r["text"]
    # 项目级在前
    assert r["text"].index("根目录规则") < r["text"].index("子目录规则")


def test_merges_user_level_last(home, ws):
    os.makedirs(os.path.join(str(home), ".agentcode"), exist_ok=True)
    with open(os.path.join(str(home), ".agentcode", "AGENTS.md"), "w", encoding="utf-8") as f:
        f.write("用户级偏好\n")
    with open(os.path.join(ws, "AGENTS.md"), "w", encoding="utf-8") as f:
        f.write("项目规则\n")
    r = pc.load(ws)
    assert "项目规则" in r["text"] and "用户级偏好" in r["text"]
    assert r["text"].index("项目规则") < r["text"].index("用户级偏好"), "项目级应排在前"


def test_ignores_claude_md(home, ws):
    """用户明确要求只认 AGENTS.md。"""
    with open(os.path.join(ws, "CLAUDE.md"), "w", encoding="utf-8") as f:
        f.write("这份不该被读到\n")
    r = pc.load(ws)
    assert "不该被读到" not in r["text"]


def test_ignores_other_filenames(home, ws):
    for name in ("AGENT.md", "agents.md", ".agents.md", "CONTRIBUTING.md"):
        with open(os.path.join(ws, name), "w", encoding="utf-8") as f:
            f.write(f"{name} 内容\n")
    # agents.md 小写也不认 —— 只认精确的 AGENTS.md
    assert "CONTRIBUTING" not in pc.load(ws)["text"]


def test_handles_none_root(home):
    r = pc.load(None)
    assert isinstance(r, dict)


def test_handles_missing_dir(home, tmp_path):
    assert pc.load(str(tmp_path / "nope"))["text"] == ""


# ============ 大小限制 ============


def test_per_file_cap(home, ws):
    with open(os.path.join(ws, "AGENTS.md"), "w", encoding="utf-8") as f:
        f.write("x" * 100000)
    r = pc.load(ws)
    assert r["truncated"] is True
    assert len(r["text"]) <= pc.MAX_BYTES_TOTAL + 200


def test_total_cap(home, ws):
    for p in (os.path.join(ws, "AGENTS.md"),):
        with open(p, "w", encoding="utf-8") as f:
            f.write("a" * pc.MAX_BYTES_PER_FILE)
    os.makedirs(os.path.join(ws, ".agentcode"), exist_ok=True)
    with open(os.path.join(ws, ".agentcode", "AGENTS.md"), "w", encoding="utf-8") as f:
        f.write("b" * pc.MAX_BYTES_PER_FILE)
    r = pc.load(ws)
    assert len(r["text"]) <= pc.MAX_BYTES_TOTAL + 400, len(r["text"])


def test_empty_file_is_skipped(home, ws):
    with open(os.path.join(ws, "AGENTS.md"), "w", encoding="utf-8") as f:
        f.write("   \n\n  ")
    assert pc.load(ws)["text"] == ""


def test_sources_report_size(home, ws):
    with open(os.path.join(ws, "AGENTS.md"), "w", encoding="utf-8") as f:
        f.write("hello")
    src = pc.load(ws)["sources"][0]
    assert src["path"].endswith("AGENTS.md")
    assert src["chars"] == 5


def test_binary_garbage_does_not_crash(home, ws):
    with open(os.path.join(ws, "AGENTS.md"), "wb") as f:
        f.write(b"\xff\xfe\x00\x01garbage")
    r = pc.load(ws)                      # errors="replace"
    assert isinstance(r["text"], str)


# ============ system 消息 ============


def test_build_system_message(home, ws):
    with open(os.path.join(ws, "AGENTS.md"), "w", encoding="utf-8") as f:
        f.write("- 提交前跑 ruff\n")
    m = pc.build_system_message(ws)
    assert m["role"] == "system"
    assert "提交前跑 ruff" in m["content"]
    assert "本项目的约定" in m["content"]


def test_build_system_message_none_without_file(home, ws):
    assert pc.build_system_message(ws) is None


def test_build_system_message_marks_truncation(home, ws):
    with open(os.path.join(ws, "AGENTS.md"), "w", encoding="utf-8") as f:
        f.write("x" * 50000)
    assert "已截断" in pc.build_system_message(ws)["content"]


# ============ target_path ============


def test_target_path_in_workspace(home, ws):
    assert pc.target_path(ws) == os.path.join(ws, "AGENTS.md")


def test_target_path_falls_back_to_home(home):
    assert ".agentcode" in pc.target_path(None)


# ============ HTTP 接口 ============


def test_api_status(client):
    ws = client.post("/workspaces", json={"name": "pc", "path": os.path.expanduser("~")}).get_json()
    r = client.get(f"/api/project_context?workspace_id={ws['id']}").get_json()
    assert r["ok"] and "target_path" in r


def test_api_save_and_read_back(client, tmp_path):
    ws = client.post("/workspaces", json={"name": "pc2", "path": os.path.expanduser("~")}).get_json()
    r = client.post("/api/project_context",
                    json={"workspace_id": ws["id"], "content": "- 约定A\n"}).get_json()
    assert r["ok"]
    target = r["path"]
    try:
        back = client.get(f"/api/project_context?workspace_id={ws['id']}").get_json()
        assert any(s["path"] == target for s in back["sources"])
    finally:
        if os.path.exists(target):
            os.remove(target)


def test_api_rejects_bad_workspace(client):
    assert client.post("/api/project_context",
                       json={"workspace_id": 999999, "content": "x"}).status_code == 400


def test_api_rejects_non_string_content(client):
    ws = client.post("/workspaces", json={"name": "pc3", "path": os.path.expanduser("~")}).get_json()
    r = client.post("/api/project_context", json={"workspace_id": ws["id"], "content": 123})
    assert r.status_code == 400


def test_api_requires_auth(anon_client):
    assert anon_client.get("/api/project_context").status_code == 401
    assert anon_client.post("/api/project_context", json={}).status_code == 401


# ============ 与 agent 主循环的集成 ============
#
# 这条链最容易在某一环断掉：会话 → 所属工作区 → 工作区路径 → 读 AGENTS.md
# → 作为 system 消息注入。任何一环断了，功能都表现为「静默不生效」。


def test_workspace_path_resolves_from_session(client, wsdir):
    import runtime

    ws = client.post("/workspaces", json={"name": "pcw", "path": wsdir}).get_json()
    assert "id" in ws, ws
    sess = client.post("/sessions", json={"workspace_id": ws["id"]}).get_json()
    runtime.set_session_id(sess["id"])
    assert os.path.realpath(runtime.get_project_root()) == os.path.realpath(wsdir)


def test_agent_injects_agents_md_system_message(client, wsdir, monkeypatch):
    """真正跑一遍 run_agent_stream，确认注入 + 发出 project_context 事件。"""
    import json

    import agent
    import runtime

    with open(os.path.join(wsdir, "AGENTS.md"), "w", encoding="utf-8") as f:
        f.write("- 这个项目的密码是 12345\n")

    ws = client.post("/workspaces", json={"name": "inj", "path": wsdir}).get_json()
    assert "id" in ws, ws
    sess = client.post("/sessions", json={"workspace_id": ws["id"]}).get_json()
    sid = sess["id"]
    runtime.set_session_id(sid)

    class _Delta:
        def __init__(self, content=None):
            self.content = content
            self.tool_calls = None
            self.reasoning_content = None

    class _Chunk:
        def __init__(self, delta):
            self.choices = [type("C", (), {"delta": delta})()]
            self.usage = None

    seen = {}

    def fake_llm(*args, **kwargs):
        seen["messages"] = kwargs.get("messages") or (args[-1] if args else [])

        def gen():
            yield _Chunk(_Delta(content="好的"))

        return gen()

    monkeypatch.setattr(agent, "get_llm_client", lambda *a, **k: object())
    monkeypatch.setattr(agent, "call_llm_with_retry", fake_llm)

    events = list(agent.run_agent_stream("你好", session_id=sid, user_api_key="k"))

    # 1) 发出了事件
    pc_events = [e for e in events if e.get("type") == "project_context"]
    assert pc_events, "应当发出 project_context 事件"
    assert pc_events[0]["sources"], pc_events[0]

    # 2) 真的进了发给 LLM 的 messages
    blob = json.dumps(seen.get("messages") or [], ensure_ascii=False)
    assert "这个项目的密码是 12345" in blob, "AGENTS.md 内容必须进入上下文"
    # 3) 而且是独立的 system 消息，不是拼进主提示词
    sys_msgs = [m for m in (seen.get("messages") or []) if m.get("role") == "system"]
    assert len(sys_msgs) >= 2, "应当是独立的第二条 system 消息"
    assert sys_msgs[1]["content"].startswith("# 本项目的约定")


def test_no_agents_md_means_no_event_and_no_extra_message(client, wsdir, monkeypatch):
    import agent
    import runtime

    ws = client.post("/workspaces", json={"name": "emp", "path": wsdir}).get_json()
    assert "id" in ws, ws
    sess = client.post("/sessions", json={"workspace_id": ws["id"]}).get_json()
    runtime.set_session_id(sess["id"])

    class _Delta:
        def __init__(self, content=None):
            self.content = content
            self.tool_calls = None
            self.reasoning_content = None

    class _Chunk:
        def __init__(self, delta):
            self.choices = [type("C", (), {"delta": delta})()]
            self.usage = None

    seen = {}

    def fake_llm(*args, **kwargs):
        seen["messages"] = kwargs.get("messages") or []
        def gen():
            yield _Chunk(_Delta(content="好的"))
        return gen()

    monkeypatch.setattr(agent, "get_llm_client", lambda *a, **k: object())
    monkeypatch.setattr(agent, "call_llm_with_retry", fake_llm)

    events = list(agent.run_agent_stream("你好", session_id=sess["id"], user_api_key="k"))
    assert not [e for e in events if e.get("type") == "project_context"]
    assert len([m for m in (seen.get("messages") or []) if m.get("role") == "system"]) == 1
