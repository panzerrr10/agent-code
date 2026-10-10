"""最小鉴权 + 服务端强制确认的回归测试。

要点：
- 没有 / 错误的 X-Auth-Token → 所有接口 401；页面与静态资源除外
- 只带 ``resume=true`` 而没有服务端签发的一次性令牌 → 拒绝执行挂起的危险操作
- 令牌一次性，重放无效；伪造 ``confirmed_delete`` 不再有任何作用
"""

import json
import os
import shutil
import tempfile
import types

import pytest

import agent
import auth
import runtime
import trash

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


def _sse(body: str):
    return [
        json.loads(line[len("data: "):])
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


@pytest.fixture
def project(monkeypatch):
    d = tempfile.mkdtemp(prefix=".agentcode-auth-", dir=HOME)
    monkeypatch.setattr(trash, "TRASH_DIR", os.path.join(d, ".trash"))
    monkeypatch.setattr(agent, "get_llm_client", lambda *a, **k: object())
    runtime.reset_all()
    runtime.set_session_id(None)
    yield d
    runtime.reset_all()
    runtime.set_session_id(None)
    shutil.rmtree(d, ignore_errors=True)


def _stub_provider(monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module, "get_provider", lambda pid=None: dict(PROVIDER_CFG))
    monkeypatch.setattr(agent, "get_provider", lambda pid=None: dict(PROVIDER_CFG))


class _Delta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning_content = None


class _Choice:
    def __init__(self, delta):
        self.delta = delta


class _Chunk:
    def __init__(self, delta):
        self.choices = [_Choice(delta)]
        self.usage = None


class _TC:
    def __init__(self, index, id=None, name=None, arguments=None):
        self.index = index
        self.id = id
        self.function = types.SimpleNamespace(name=name, arguments=arguments)


def _llm(chunks):
    def fake(*args, **kwargs):
        def gen():
            yield from chunks

        return gen()

    return fake


# ============ 鉴权 ============


def test_public_paths_need_no_token(anon_client):
    assert anon_client.get("/").status_code == 200
    assert anon_client.get("/favicon.ico").status_code == 200
    assert anon_client.get("/static/vendor/marked.min.js").status_code == 200


def test_api_endpoints_require_token(anon_client):
    assert anon_client.get("/api/providers").status_code == 401
    assert anon_client.get("/api/project_tree").status_code == 401
    assert anon_client.get("/sessions").status_code == 401
    assert anon_client.post("/sessions").status_code == 401
    assert anon_client.get("/documents").status_code == 401
    assert anon_client.get("/export/1").status_code == 401
    assert anon_client.post("/run/cancel", json={"session_id": 1}).status_code == 401
    assert (
        anon_client.post("/run", json={"task": "x", "session_id": 1}).status_code == 401
    )


def test_wrong_token_is_rejected(anon_client):
    anon_client.environ_base["HTTP_X_AUTH_TOKEN"] = "definitely-not-the-token"
    assert anon_client.get("/api/providers").status_code == 401


def test_correct_token_is_accepted(client):
    assert client.get("/api/providers").status_code == 200
    assert client.get("/sessions").status_code == 200


def test_page_embeds_token_for_same_origin_frontend(anon_client):
    body = anon_client.get("/").get_data(as_text=True)
    assert auth.get_token() in body, "页面没有注入访问令牌，同源前端将无法调用接口"
    assert "X-Auth-Token" in body, "页面缺少自动携带令牌的逻辑"


def test_auth_token_source_env_override(monkeypatch):
    monkeypatch.setenv("AGENT_AUTH_TOKEN", "env-provided-token")
    auth.reset_cache()
    try:
        assert auth.get_token() == "env-provided-token"
        assert auth.verify("env-provided-token") is True
        assert auth.verify("something-else") is False
    finally:
        auth.reset_cache()


# ============ 一次性确认令牌（单元语义）============


def test_confirm_token_is_one_time():
    runtime.set_pending_state({"confirm_token": "abc"}, "s1")
    assert runtime.verify_and_consume_confirm_token("s1", "abc") is True
    # 已消费：重放必须失败
    assert runtime.verify_and_consume_confirm_token("s1", "abc") is False


def test_confirm_token_rejects_missing_wrong_or_other_session():
    runtime.set_pending_state({"confirm_token": "abc"}, "s1")
    assert runtime.verify_and_consume_confirm_token("s1", None) is False
    assert runtime.verify_and_consume_confirm_token("s1", "wrong") is False
    assert runtime.verify_and_consume_confirm_token("s2", "abc") is False
    # 失败不应消费掉令牌
    assert runtime.verify_and_consume_confirm_token("s1", "abc") is True


# ============ 端到端：必须出示令牌才能续跑 ============


def _start_pending_write(client, project, monkeypatch, target):
    """跑一轮，让 Agent 停在「等待确认 write_file」。返回 (session_id, 事件列表)。"""
    _stub_provider(monkeypatch)
    with open(target, "w", encoding="utf-8") as f:
        f.write("OLD")

    monkeypatch.setattr(
        agent,
        "call_llm_with_retry",
        _llm(
            [
                _Chunk(
                    _Delta(
                        tool_calls=[
                            _TC(
                                0,
                                id="call_1",
                                name="write_file",
                                arguments=json.dumps(
                                    {"path": target, "content": "NEW"}, ensure_ascii=False
                                ),
                            )
                        ]
                    )
                )
            ]
        ),
    )
    sid = client.post("/sessions").get_json()["id"]
    resp = client.post(
        "/run",
        json={
            "task": "改一下配置",
            "session_id": sid,
            "provider_id": "deepseek",
            "project_root": project,
        },
    )
    events = _sse(resp.get_data(as_text=True))
    return sid, events


def test_resume_without_token_is_rejected(client, project, monkeypatch):
    target = os.path.join(project, "cfg.txt")
    sid, events = _start_pending_write(client, project, monkeypatch, target)

    assert any(e["type"] == "confirm_write" for e in events), events
    assert open(target, encoding="utf-8").read() == "OLD"

    # 1) 只带 resume=true → 拒绝
    resp = client.post("/run", json={"task": "", "session_id": sid, "resume": True})
    ev = _sse(resp.get_data(as_text=True))
    assert ev and ev[0]["type"] == "error", ev
    assert "令牌" in ev[0]["content"]
    assert open(target, encoding="utf-8").read() == "OLD", "无令牌竟执行了挂起的写操作"

    # 2) 伪造旧的客户端字段也无效（该字段已被服务端忽略）
    resp2 = client.post(
        "/run",
        json={
            "task": "",
            "session_id": sid,
            "resume": True,
            "confirmed_delete": True,
            "auto_confirm": True,
        },
    )
    ev2 = _sse(resp2.get_data(as_text=True))
    assert ev2 and ev2[0]["type"] == "error", ev2
    assert open(target, encoding="utf-8").read() == "OLD"


def test_resume_with_valid_token_executes_and_cannot_be_replayed(
    client, project, monkeypatch
):
    target = os.path.join(project, "cfg.txt")
    sid, events = _start_pending_write(client, project, monkeypatch, target)

    confirm = next(e for e in events if e["type"] == "confirm_write")
    token = confirm.get("confirm_token")
    assert token, "确认事件没有携带一次性令牌，前端将无法完成确认"

    # 带正确令牌续跑 → 执行写入，然后模型给出最终答复
    monkeypatch.setattr(
        agent, "call_llm_with_retry", _llm([_Chunk(_Delta(content="已经改好了"))])
    )
    resp = client.post(
        "/run",
        json={
            "task": "",
            "session_id": sid,
            "resume": True,
            "confirm_token": token,
            "project_root": project,
        },
    )
    ev = _sse(resp.get_data(as_text=True))
    assert any(e["type"] == "tool_result" for e in ev), ev
    assert open(target, encoding="utf-8").read() == "NEW", "带令牌的续跑没有真正执行"

    # 令牌一次性：重放必须失败
    resp2 = client.post(
        "/run",
        json={
            "task": "",
            "session_id": sid,
            "resume": True,
            "confirm_token": token,
            "project_root": project,
        },
    )
    ev2 = _sse(resp2.get_data(as_text=True))
    assert ev2 and ev2[0]["type"] == "error", ev2


def test_agent_confirm_event_carries_fresh_token_each_time(project, monkeypatch):
    """两次不同的确认应得到不同的令牌（不是固定值）。"""
    monkeypatch.setattr(agent, "get_llm_client", lambda *a, **k: object())
    tokens = []
    for name, target in (("a.txt", "A"), ("b.txt", "B")):
        path = os.path.join(project, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write("old")
        monkeypatch.setattr(
            agent,
            "call_llm_with_retry",
            _llm(
                [
                    _Chunk(
                        _Delta(
                            tool_calls=[
                                _TC(
                                    0,
                                    id="c1",
                                    name="write_file",
                                    arguments=json.dumps({"path": path, "content": target}),
                                )
                            ]
                        )
                    )
                ]
            ),
        )
        runtime.set_session_id("tok-sess")
        runtime.set_project_root(project)
        events = list(agent.run_agent_stream("写文件", session_id="tok-sess", user_api_key="k"))
        ev = next(e for e in events if e["type"] == "confirm_write")
        tokens.append(ev["confirm_token"])

    assert tokens[0] and tokens[1]
    assert tokens[0] != tokens[1], "两次确认复用了同一个令牌"
