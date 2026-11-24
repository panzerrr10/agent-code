
"""Flask 路由 + SSE 流式协议的集成测试。

特点：**不访问网络、不依赖 .env**。LLM 调用与 provider 配置都在用例内打桩，
因此可以放进 CI 稳定运行；同时走的是真实的 Flask 路由、真实的 Agent 主循环
与真实的 SSO 分帧逻辑，而不是 mock 掉整个接口。
"""

import io
import json
import os
import tempfile

import pytest

import app as app_module
import auth
import runtime
from tests.fakes import patch_agent

HOME = os.path.expanduser("~")
PROVIDER_CFG = {
    "api_key": "sk-test",
    "base_url": "https://api.deepseek.com/v1",
    "name": "DeepSeek",
    "icon": "🐳",
    "models": ["deepseek-chat"],
    "default_model": "deepseek-chat",
    "supports_embedding": False,
}


# ============ 打桩工具 ============


def _stub_provider(monkeypatch):
    """让 provider 与 LLM 客户端脱离 .env / 网络。"""
    import agent

    monkeypatch.setattr(app_module, "get_provider", lambda pid=None: dict(PROVIDER_CFG))
    monkeypatch.setattr(agent, "get_provider", lambda pid=None: dict(PROVIDER_CFG))


class _FakeDelta:
    def __init__(self, content=None):
        self.content = content
        self.tool_calls = None
        self.reasoning_content = None


class _FakeChoice:
    def __init__(self, delta):
        self.delta = delta


class _FakeChunk:
    def __init__(self, content):
        self.choices = [_FakeChoice(_FakeDelta(content))]
        self.usage = None


def _stub_llm(monkeypatch, texts):
    """按顺序吐出固定文本，模拟 Executor 的每一次 LLM 调用。"""

    queue = list(texts)

    def fake(*args, **kwargs):
        text = queue.pop(0) if queue else "（结束）"

        def gen():
            yield _FakeChunk(text)

        return gen()

    patch_agent(monkeypatch, fake)


def _sse(body: str):
    """解析 SSE 响应体为事件列表。"""
    return [
        json.loads(line[len("data: "):])
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


@pytest.fixture
def client():
    runtime.reset_all()
    runtime.set_session_id(None)
    c = app_module.app.test_client()
    c.environ_base["HTTP_X_AUTH_TOKEN"] = auth.get_token()
    yield c
    runtime.reset_all()
    runtime.set_session_id(None)


def _new_session(client) -> int:
    return client.post("/sessions").get_json()["id"]


# ============ 会话与基础接口 ============


def test_session_lifecycle_and_history(client):
    sid = _new_session(client)
    assert any(s["id"] == sid for s in client.get("/sessions").get_json())
    assert client.get(f"/history/{sid}").get_json() == []

    assert client.delete(f"/sessions/{sid}").status_code == 200
    assert not any(s["id"] == sid for s in client.get("/sessions").get_json())


def test_providers_endpoint_never_leaks_api_key(client):
    """provider 列表绝不能带 api_key。

    **不要依赖 .env**：这个测试原来断言 `default == "deepseek"`，
    而那个默认值是 .env 迁移出来的 —— .env 被 gitignore，干净检出里没有，
    于是「本地全绿、CI 必红」。这里自己建一个提供商，跑在哪儿结果都一样。
    """
    secret = "sk-leak-check-abcdefghijklmnop123456"
    r = client.post("/api/providers", json={
        "name": "泄露检查", "base_url": "https://x.example/v1",
        "api_key": secret, "models": ["m-1"],
    })
    assert r.get_json()["ok"]

    new_id = r.get_json()["providers"][0]["id"]
    body = client.get("/api/providers").get_json()
    assert body["default"] == new_id, f"新建的第一个提供商应成为默认：{body['default']}"
    assert body["providers"], "应该能读回刚建的提供商"
    for p in body["providers"]:
        assert "api_key" not in p, "provider 列表泄露了 api_key"
        assert {"id", "name", "models", "default_model", "has_key"} <= set(p)
    # 整个响应体里也不能出现明文密钥
    assert secret not in client.get("/api/providers").get_data(as_text=True)


def test_permission_gates_on_read_endpoints(client):
    assert client.get("/api/project_tree?path=/etc").status_code == 403
    assert client.get("/api/project_tree?path=/root").status_code == 403
    assert client.get("/api/file_content").status_code == 400
    assert (
        client.get("/api/file_content", query_string={"path": "/etc/passwd"}).status_code
        == 403
    )
    assert (
        client.get(
            "/api/file_content", query_string={"path": os.path.join(HOME, ".ssh", "id_rsa")}
        ).status_code
        == 403
    )


def test_project_tree_and_file_content_happy_path(client):
    tmp = tempfile.mkdtemp(prefix=".agentcode-route-", dir=HOME)
    path = os.path.join(tmp, "hello.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("路由集成测试内容")
    try:
        assert client.get("/api/project_tree", query_string={"path": tmp}).status_code == 200
        resp = client.get("/api/file_content", query_string={"path": path})
        assert resp.status_code == 200
        assert resp.get_json()["content"] == "路由集成测试内容"
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def test_execute_endpoint_is_gone(client):
    resp = client.post(
        "/execute", json={"session_id": 1, "tool_name": "list_dir", "tool_args": {}}
    )
    assert resp.status_code == 404


# ============ /run 的 SSE 协议 ============


def test_run_sse_framing_and_persistence(client, monkeypatch):
    _stub_provider(monkeypatch)
    _stub_llm(monkeypatch, ["你好，我是 Agent"])

    sid = _new_session(client)
    resp = client.post(
        "/run", json={"task": "打个招呼", "session_id": sid, "provider_id": "deepseek"}
    )
    assert resp.status_code == 200
    assert resp.mimetype == "text/event-stream"

    body = resp.get_data(as_text=True)
    # SSE 分帧：每条事件一行 data:，事件之间以空行分隔
    assert "data: " in body and "\n\n" in body

    events = _sse(body)
    types = [e["type"] for e in events]
    assert "text" in types
    assert types[-1] == "done"
    assert "".join(e["content"] for e in events if e["type"] == "text") == "你好，我是 Agent"

    # 落库：用户消息 + assistant 消息
    msgs = client.get(f"/history/{sid}").get_json()
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert "你好，我是 Agent" in msgs[1]["content"]

    client.delete(f"/sessions/{sid}")


def test_run_rejects_empty_task_and_missing_session(client):
    resp = client.post("/run", json={"task": "", "session_id": None})
    assert resp.status_code == 200  # SSE 通道内返回 error 事件
    events = _sse(resp.get_data(as_text=True))
    assert events and events[0]["type"] == "error"


def test_run_sse_emits_cancelled_when_interrupted(client, monkeypatch):
    _stub_provider(monkeypatch)
    holder = {}


    def fake(*args, **kwargs):
        def gen():
            yield _FakeChunk("前半段输出")
            runtime.request_cancel(holder["sid"])  # 模拟用户点了「停止」
            yield _FakeChunk("这段不应出现")

        return gen()

    patch_agent(monkeypatch, fake)

    sid = _new_session(client)
    holder["sid"] = sid
    resp = client.post(
        "/run", json={"task": "长任务", "session_id": sid, "provider_id": "deepseek"}
    )
    events = _sse(resp.get_data(as_text=True))
    types = [e["type"] for e in events]

    assert "cancelled" in types, f"应在 SSE 中下发 cancelled，实际：{types}"
    assert types[-1] == "done"
    assert not any("这段不应出现" in str(e.get("content", "")) for e in events)

    client.delete(f"/sessions/{sid}")


# ============ /run_multi 多 Agent 流水线 ============


def test_run_multi_emits_phases_plan_and_review(client, monkeypatch):
    _stub_provider(monkeypatch)
    _stub_llm(monkeypatch, ["执行完成"])

    import planner
    import reviewer

    monkeypatch.setattr(
        planner, "create_plan", lambda *a, **k: {"steps": ["第一步", "第二步"]}
    )
    monkeypatch.setattr(
        reviewer,
        "review",
        lambda *a, **k: {"passed": True, "reason": "符合预期", "suggestions": []},
    )

    sid = _new_session(client)
    resp = client.post(
        "/run_multi",
        json={
            "task": "做点事情",
            "session_id": sid,
            "provider_id": "deepseek",
            "planner_provider_id": "deepseek",
            "reviewer_provider_id": "deepseek",
        },
    )
    assert resp.status_code == 200
    events = _sse(resp.get_data(as_text=True))
    types = [e["type"] for e in events]

    assert "phase" in types and "plan_from_planner" in types and "review_result" in types
    assert types[-1] == "done"

    phases = [(e["name"], e["status"]) for e in events if e["type"] == "phase"]
    assert ("planner", "doing") in phases
    assert ("executor", "done") in phases
    assert ("reviewer", "done") in phases

    plan_ev = next(e for e in events if e["type"] == "plan_from_planner")
    assert plan_ev["plan"]["steps"] == ["第一步", "第二步"]
    assert next(e for e in events if e["type"] == "review_result")["result"]["passed"] is True

    client.delete(f"/sessions/{sid}")


# ============ 导出 ============


def test_export_markdown_and_json(client):
    import db

    sid = _new_session(client)
    db.save_message(sid, "user", "你好")
    db.save_message(
        sid,
        "assistant",
        json.dumps({"answer": "你好呀", "tools": [], "tokens": None}, ensure_ascii=False),
    )

    md = client.get(f"/export/{sid}?format=md")
    assert md.status_code == 200
    assert "text/markdown" in md.headers["Content-Type"]
    md_text = md.get_data(as_text=True)
    assert "你好呀" in md_text and "导出时间" in md_text

    js = client.get(f"/export/{sid}?format=json")
    data = json.loads(js.get_data(as_text=True))
    assert [m["role"] for m in data["messages"]] == ["user", "assistant"]
    assert data["messages"][1]["content"] == "你好呀"

    client.delete(f"/sessions/{sid}")


# ============ 知识库文档管理（打桩向量库，避免真实 embedding 请求）============


def test_documents_upload_list_delete(client, monkeypatch):
    import rag

    monkeypatch.setattr(rag, "add_document_to_vectorstore", lambda *a, **k: 3)
    monkeypatch.setattr(rag, "delete_document_from_vectorstore", lambda *a, **k: None)

    resp = client.post(
        "/upload_doc",
        data={"file": (io.BytesIO("这是测试文档的正文".encode()), "t.txt")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    payload = resp.get_json()
    assert payload["chunk_count"] == 3
    assert payload["filename"] == "t.txt"
    doc_id = payload["doc_id"]

    assert any(d["id"] == doc_id for d in client.get("/documents").get_json())
    assert client.delete(f"/documents/{doc_id}").status_code == 200
    assert not any(d["id"] == doc_id for d in client.get("/documents").get_json())


def test_upload_rejects_unsupported_and_empty(client):
    resp = client.post(
        "/upload_doc",
        data={"file": (io.BytesIO(b"binary"), "evil.exe")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400

    resp2 = client.post(
        "/upload_doc",
        data={"file": (io.BytesIO(b""), "empty.txt")},
        content_type="multipart/form-data",
    )
    assert resp2.status_code == 400
