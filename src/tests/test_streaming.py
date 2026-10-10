import sys
import time

sys.path.insert(0, "src")

import agent  # noqa: E402
import runtime  # noqa: E402

SESSION = "s-stream"
class _Delta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning_content = None
class _Chunk:
    def __init__(self, content=None, tool_calls=None, usage=None):
        self.choices = [type("C", (), {"delta": _Delta(content, tool_calls)})()]
        self.usage = usage
def _run(monkeypatch, chunks, *, delay=0.0):
    """跑一次 run_agent_stream，返回 (事件列表, 累积文本)。"""
    def fake(*a, **k):
        def gen():
            for c in chunks:
                if delay:
                    time.sleep(delay)
                yield c
        return gen()
    monkeypatch.setattr(agent, "get_llm_client", lambda *a, **k: object())
    monkeypatch.setattr(agent, "call_llm_with_retry", fake)
    monkeypatch.setattr(agent, "get_provider", lambda pid=None: {
        "id": "t", "name": "t", "base_url": "https://x/v1", "api_key": "k",
        "models": ["m"], "default_model": "m", "supports_embedding": False,
    })
    runtime.set_session_id(SESSION)
    runtime.set_run_index(1)
    events = list(agent.run_agent_stream("任务", session_id=SESSION, user_api_key="k"))
    text = "".join(e.get("content", "") for e in events if e.get("type") == "text")
    return events, text
def _words(n, delay):
    """n 个分片，每片一个词，片间 sleep(delay)。"""
    return [_Chunk(f"词{i} ") for i in range(n)]
def test_multi_chunk_stream_is_not_duplicated(monkeypatch):
    """**最关键的一条**：分多片流式时，内容不能重复也不能少。"""
    import agent as a
    def fake(*args, **kwargs):
        def gen():
            for i in range(10):
                time.sleep(a.STREAM_FLUSH_SEC * 1.2)   # 保证跨过节流阈值
                yield _Chunk(f"片段{i} ")
        return gen()
    monkeypatch.setattr(agent, "get_llm_client", lambda *x, **k: object())
    monkeypatch.setattr(agent, "call_llm_with_retry", fake)
    monkeypatch.setattr(agent, "get_provider", lambda pid=None: {
        "id": "t", "name": "t", "base_url": "https://x/v1", "api_key": "k",
        "models": ["m"], "default_model": "m", "supports_embedding": False,
    })
    runtime.set_session_id(SESSION)
    runtime.set_run_index(1)
    events = list(agent.run_agent_stream("任务", session_id=SESSION, user_api_key="k"))
    got = "".join(e.get("content", "") for e in events if e.get("type") == "text")
    expect = "".join(f"片段{i} " for i in range(10))
    assert got == expect, f"内容不一致：\n得到 {got!r}\n期望 {expect!r}"
def test_slow_stream_produces_multiple_text_events(monkeypatch):
    """慢速流必须真的分多次发 —— 否则等于没做流式。"""
    def fake(*a, **k):
        def gen():
            for i in range(8):
                time.sleep(agent.STREAM_FLUSH_SEC * 1.5)
                yield _Chunk(f"x{i}")
        return gen()
    monkeypatch.setattr(agent, "get_llm_client", lambda *a, **k: object())
    monkeypatch.setattr(agent, "call_llm_with_retry", fake)
    monkeypatch.setattr(agent, "get_provider", lambda pid=None: {
        "id": "t", "name": "t", "base_url": "https://x/v1", "api_key": "k",
        "models": ["m"], "default_model": "m", "supports_embedding": False,
    })
    runtime.set_session_id(SESSION)
    runtime.set_run_index(1)
    events = list(agent.run_agent_stream("任务", session_id=SESSION, user_api_key="k"))
    texts = [e for e in events if e.get("type") == "text"]
    assert len(texts) >= 4, f"只发了 {len(texts)} 段，流式没生效"
    assert "".join(t["content"] for t in texts) == "".join(f"x{i}" for i in range(8))
def test_single_chunk_stream_behaves_exactly_as_before(monkeypatch):
    """**兼容性**：假 LLM 每次只吐一个 chunk，行为必须和改之前完全一样。
    否则现有那一批基于假 LLM 的测试（取消/续跑/路由）会莫名其妙地失败。
    """
    events, text = _run(monkeypatch, [_Chunk("已经改好了")])
    texts = [e for e in events if e.get("type") == "text"]
    assert len(texts) == 1, f"单块流应当只发一条 text，实际 {len(texts)} 条"
    assert text == "已经改好了"
def test_flush_happens_even_if_below_threshold(monkeypatch):
    """节流阈值内结束的流，最后那一段也必须冲出去，不能丢。"""
    events, text = _run(monkeypatch, [_Chunk("短")], delay=0)
    assert text == "短", "低于节流阈值的内容被丢掉了"
def test_many_small_chunks_total_is_exact(monkeypatch):
    """很多小块（快流）也不能多一个字符或少一个字符。"""
    chunks = [_Chunk(f"{i}") for i in range(200)]
    _, text = _run(monkeypatch, chunks)
    assert text == "".join(str(i) for i in range(200))
def test_streaming_constant_is_sane():
    """节流间隔要合理：太小会刷爆 SSE，太大就不像流式了。"""
    assert 0.01 <= agent.STREAM_FLUSH_SEC <= 0.2, agent.STREAM_FLUSH_SEC
