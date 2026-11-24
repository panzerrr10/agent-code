
"""prompt 缓存命中统计的采集。

**为什么要先做这个**：多步 Agent 每一步都把「系统提示 + 工具 schema + 代码库索引
+ 历史」重发一遍，固定前缀占了单步 prompt 的一大截。各家都提供前缀缓存
（命中部分价格低得多、也更快），但前提是前缀**逐字节不变**。

此前我们**完全没采集**这些字段，于是「缓存到底有没有命中」是盲区 ——
不知道命中率，就没法判断压缩、注入、工具集变更是不是在破坏前缀。
这一步只做「看得见」。

各家字段名不同且都是可选的，所以全程要能容错：
    DeepSeek : prompt_cache_hit_tokens / prompt_cache_miss_tokens
    OpenAI   : prompt_tokens_details.cached_tokens
    百炼/智谱 : 可能一个都没有
"""

import types

import pytest

from tests.fakes import patch_agent
from utils import cache_hit_rate, extract_cache_stats


def _usage(**kw):
    return types.SimpleNamespace(**kw)


# ============ DeepSeek 形状 ============


def test_deepseek_shape():
    u = _usage(prompt_tokens=1000, prompt_cache_hit_tokens=800,
               prompt_cache_miss_tokens=200)
    s = extract_cache_stats(u)
    assert s["cache_hit_tokens"] == 800
    assert s["cache_miss_tokens"] == 200


def test_deepseek_missing_miss_is_derived():
    """只报 hit 时，miss 用 prompt - hit 推出来。"""
    u = _usage(prompt_tokens=1000, prompt_cache_hit_tokens=750)
    s = extract_cache_stats(u)
    assert s["cache_hit_tokens"] == 750
    assert s["cache_miss_tokens"] == 250


# ============ OpenAI 形状 ============


def test_openai_shape():
    u = _usage(prompt_tokens=1000,
               prompt_tokens_details=types.SimpleNamespace(cached_tokens=600))
    s = extract_cache_stats(u)
    assert s["cache_hit_tokens"] == 600
    assert s["cache_miss_tokens"] == 400


def test_dict_shape():
    """有些封装会把 usage 转成 dict，两种都要认。"""
    s = extract_cache_stats({"prompt_tokens": 500, "prompt_cache_hit_tokens": 100})
    assert s == {"cache_hit_tokens": 100, "cache_miss_tokens": 400}


# ============ 缺字段 / 边界 ============


def test_provider_without_cache_fields():
    """百炼/智谱可能一个字段都没有 —— 不能崩，也不该谎报命中。"""
    s = extract_cache_stats(_usage(prompt_tokens=1234))
    assert s == {"cache_hit_tokens": 0, "cache_miss_tokens": 0}


def test_none_usage():
    assert extract_cache_stats(None) == {"cache_hit_tokens": 0, "cache_miss_tokens": 0}


def test_none_inside_details():
    s = extract_cache_stats(_usage(prompt_tokens=10, prompt_tokens_details=None))
    assert s["cache_hit_tokens"] == 0


def test_hit_larger_than_prompt_does_not_go_negative():
    """厂商数据异常时 miss 不能变负数。"""
    s = extract_cache_stats(_usage(prompt_tokens=100, prompt_cache_hit_tokens=500))
    assert s["cache_miss_tokens"] == 0


def test_string_numbers_are_coerced():
    s = extract_cache_stats(_usage(prompt_tokens="100",
                                   prompt_cache_hit_tokens="40"))
    assert s["cache_hit_tokens"] == 40


# ============ 命中率口径 ============


def test_hit_rate_uses_prompt_tokens_as_denominator():
    """分母必须是 prompt_tokens，不能是 hit+miss。

    用 hit+miss 当分母时，只要厂商少报一个字段就会算成 100% ——
    是个很容易骗过自己的口径错误。
    """
    assert cache_hit_rate(800, 1000) == pytest.approx(0.8)
    assert cache_hit_rate(50, 1000) == pytest.approx(0.05)


def test_hit_rate_none_when_no_data():
    """没有数据要返回 None（界面显示「未上报」），而不是 0%。"""
    assert cache_hit_rate(0, 0) is None
    assert cache_hit_rate(None, None) is None
    assert cache_hit_rate(5, 0) is None


def test_hit_rate_clamped():
    assert cache_hit_rate(2000, 1000) == 1.0
    assert cache_hit_rate(-5, 1000) == 0.0


def test_hit_rate_bad_input():
    assert cache_hit_rate("abc", 100) is None


# ============ 端到端：主循环必须把命中率发出来 ============


def test_agent_emits_cache_stats(monkeypatch):
    """跑一遍主循环，token_usage 事件里必须带缓存字段。"""
    import types as _types

    import agent
    import db
    import runtime

    runtime.reset_all()
    sid = db.create_session("cache-test")
    db.set_permission_level(sid, 3)
    runtime.reset_all()

    class _Delta:
        def __init__(self):
            self.content = "好了"
            self.tool_calls = None
            self.reasoning_content = None

    class _Chunk:
        def __init__(self):
            self.choices = [_types.SimpleNamespace(delta=_Delta())]
            self.usage = _types.SimpleNamespace(
                prompt_tokens=1000, completion_tokens=10, total_tokens=1010,
                prompt_cache_hit_tokens=800, prompt_cache_miss_tokens=200,
            )

    patch_agent(monkeypatch, lambda *a, **k: iter([_Chunk()]))

    events = list(agent.run_agent_stream("干活", session_id=sid, user_api_key="k"))
    usage_ev = next(e for e in events if e["type"] == "token_usage")
    assert usage_ev["cache_hit_tokens"] == 800
    assert usage_ev["cache_miss_tokens"] == 200
    assert usage_ev["cache_hit_rate"] == pytest.approx(0.8)
    runtime.reset_all()
