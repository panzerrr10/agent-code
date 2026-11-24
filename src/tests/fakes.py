"""测试用的假模型。

## 为什么需要这一层

重构前，测试通过 monkeypatch 掉 ``agent.call_llm_with_retry`` 并手搓
OpenAI 的 ``chunk.choices[0].delta`` 结构来模拟模型。现在生产代码走
LangChain（``ChatOpenAI`` + ``AIMessageChunk``），这些假对象不再匹配。

这里提供两个**适配器**，把老的假流式/假响应包装成 LangChain 模型，
于是那几十个用例的意图（权限、确认、续跑、取消……）可以原样保留，
不必为了换个模型层把它们全部重写。

- ``LegacyChunkModel``：包装「按调用次数返回一串 OpenAI chunk」的假流
- ``LegacyResponseChat``：包装「直接返回一个 OpenAI response 对象」的假响应
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult


def _usage_meta(usage) -> dict:
    """把 OpenAI 形状的 usage 映射成 LangChain 的 usage_metadata。"""
    def pick(*names):
        for n in names:
            v = usage.get(n) if isinstance(usage, dict) else getattr(usage, n, None)
            if v is not None:
                return v
        return None

    prompt = int(pick("prompt_tokens", "input_tokens") or 0)
    completion = int(pick("completion_tokens", "output_tokens") or 0)
    hit = int(pick("prompt_cache_hit_tokens", "cached_tokens") or 0)
    return {
        "input_tokens": prompt,
        "output_tokens": completion,
        "total_tokens": int(pick("total_tokens") or (prompt + completion)),
        "input_token_details": {"cache_read": hit},
    }


def _safe_args(raw: str) -> dict:
    """模型偶尔给出非法 JSON（真实场景也会），测试要覆盖这种情况而不是崩掉。"""
    try:
        return json.loads(raw or "{}")
    except Exception:                                        # noqa: BLE001
        return {}


class _Base(BaseChatModel):
    source: Any = None

    @property
    def _llm_type(self) -> str:
        return "legacy-fake"

    def bind_tools(self, tools, **kwargs):                   # noqa: ARG002
        return self

    def _materialize(self, messages=None):
        """取出假的响应源。

        旧用例的假函数会从 ``kwargs["messages"]`` 里读「实际发出去的对话」
        来做断言，所以这里把 LangChain 消息转回 OpenAI dict 一起传进去。
        """
        if not callable(self.source):
            return self.source
        if messages is not None:
            import llm

            try:
                return self.source(messages=llm.messages_to_dicts(messages), model="fake")
            except TypeError:
                pass
        return self.source()


class LegacyChunkModel(_Base):
    """把「假的 OpenAI 流式 chunk」包装成 LangChain 聊天模型。"""

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ARG002
        usage = None
        # 惰性迭代：旧用例靠 chunk 之间的 sleep 验证节流，提前 list() 会把
        # 时间差抹平，测出来的就不是真实流式行为了。
        for chunk in self._materialize(messages):
            delta = chunk.choices[0].delta
            content = getattr(delta, "content", None)
            if content:
                yield ChatGenerationChunk(message=AIMessageChunk(content=content))

            usage = getattr(chunk, "usage", None) or usage
            tool_calls = getattr(delta, "tool_calls", None) or []
            if tool_calls:
                pieces = []
                for i, tc in enumerate(tool_calls):
                    fn = getattr(tc, "function", None)
                    pieces.append(
                        {
                            "name": getattr(fn, "name", "") if fn else "",
                            "args": (getattr(fn, "arguments", "") if fn else "") or "{}",
                            "id": getattr(tc, "id", "") or "",
                            "index": getattr(tc, "index", i) if getattr(tc, "index", None) is not None else i,
                        }
                    )
                yield ChatGenerationChunk(
                    message=AIMessageChunk(content="", tool_call_chunks=pieces)
                )

        if usage is not None:
            yield ChatGenerationChunk(message=AIMessageChunk(content="", usage_metadata=_usage_meta(usage)))

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        content = ""
        tool_calls: list[dict] = []
        for chunk in self._materialize(messages):
            delta = chunk.choices[0].delta
            content += getattr(delta, "content", None) or ""
            for tc in getattr(delta, "tool_calls", None) or []:
                fn = getattr(tc, "function", None)
                tool_calls.append(
                    {
                        "name": getattr(fn, "name", "") if fn else "",
                        "args": _safe_args(getattr(fn, "arguments", "") if fn else ""),
                        "id": getattr(tc, "id", "") or "",
                        "type": "tool_call",
                    }
                )
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=content, tool_calls=tool_calls))]
        )


class LegacyResponseChat(_Base):
    """把「假的 OpenAI response 对象」包装成 LangChain 聊天模型。"""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        resp = self._materialize()
        msg = resp.choices[0].message
        tool_calls = []
        for tc in getattr(msg, "tool_calls", None) or []:
            fn = getattr(tc, "function", None)
            tool_calls.append(
                {
                    "name": getattr(fn, "name", "") if fn else "",
                    "args": _safe_args(getattr(fn, "arguments", "") if fn else ""),
                    "id": getattr(tc, "id", "") or "",
                    "type": "tool_call",
                }
            )
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(content=getattr(msg, "content", "") or "", tool_calls=tool_calls)
                )
            ]
        )


def patch_agent(monkeypatch, source) -> None:
    """让 agent 使用给定的假流（替换模型工厂）。"""
    import agent

    monkeypatch.setattr(agent, "_build_model", lambda *a, **k: LegacyChunkModel(source=source))


def patch_mini(monkeypatch, source) -> None:
    """让 mini_agent 使用给定的假响应（替换模型工厂）。"""
    import mini_agent

    monkeypatch.setattr(mini_agent, "build_chat", lambda **k: LegacyResponseChat(source=source))
