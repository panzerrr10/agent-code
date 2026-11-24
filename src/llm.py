"""模型层：统一走 LangChain 的 ``ChatOpenAI``。

## 为什么不再直接用 openai SDK

原来每一步都要手工解析 ``chunk.choices[0].delta``、自己拼 tool_calls 缓冲区、
自己算 usage。换成 LangChain 之后：

- 流式聚合由 ``AIMessageChunk.__add__`` 负责，``tool_calls`` 直接就是解析好的 dict
- 工具用 ``bind_tools`` 绑定，schema 由 pydantic 生成，不再手写 JSON Schema
- ``usage_metadata`` 是跨厂商归一化的，不用为每家写 if/else

## 仍然保留的两个「厂商特化」

1. **``reasoning_content``（DeepSeek 推理模型）**：LangChain 会把它放进
   ``additional_kwargs``。回传时若缺字段，DeepSeek 直接 400，所以本模块负责
   在 dict 与 AIMessage 之间双向搬运这个字段。
2. **``extra_body``（厂商专属参数）**：如 DeepSeek 的 ``enable_search``，
   发给别家会 400，因此只在对应 provider 上挂。
"""

from __future__ import annotations

import logging
import os
from typing import Any

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_openai import ChatOpenAI

logger = logging.getLogger(__name__)

# 单次请求超时。SDK 默认 600 秒，配合重试最坏会拖十几分钟，
# 期间用户点「停止」也没反馈（取消只在收到 chunk 时检查）。
DEFAULT_TIMEOUT = float(os.getenv("AGENT_LLM_TIMEOUT", "120"))


def build_chat_model(
    *,
    api_key: str,
    base_url: str,
    model: str,
    extra_body: dict | None = None,
    timeout: float | None = None,
    streaming: bool = True,
) -> ChatOpenAI:
    """构造一个 OpenAI 兼容的聊天模型。

    Args:
        api_key: 该 provider 的 key（本地模型如 Ollama 可以为空）。
        base_url: OpenAI 兼容端点，例如 ``https://api.deepseek.com/v1``。
        model: 模型名。
        extra_body: 厂商专属请求字段（只对该 provider 生效）。
        timeout: 单次请求超时秒数。
        streaming: 是否开启流式。

    Returns:
        可直接 ``.stream()`` / ``.bind_tools()`` 的 LangChain 聊天模型。
    """
    model_kwargs: dict[str, Any] = {}
    if extra_body:
        model_kwargs["extra_body"] = extra_body

    return ChatOpenAI(
        model=model,
        api_key=api_key or "not-needed",      # Ollama 等本地模型不校验，但不能传空串
        base_url=base_url or None,
        temperature=0,
        timeout=timeout or DEFAULT_TIMEOUT,
        max_retries=0,                        # 重试统一收敛在 utils.call_with_retry
        streaming=streaming,
        stream_usage=True,                    # 让最后一块 chunk 带上 usage
        model_kwargs=model_kwargs,
    )


# ============ dict <-> LangChain 消息 ============


def dict_to_message(m: dict) -> BaseMessage:
    """把 OpenAI 格式的 dict 转成 LangChain 消息。

    消息在本项目里始终以 **OpenAI dict 格式** 存储 —— 因为上下文压缩、
    断点续跑、落库格式全都建立在这个结构上。这里只做「发给模型前的临时转换」。
    """
    role = m.get("role")
    content = m.get("content", "")

    if role == "system":
        return SystemMessage(content=content)

    if role == "user":
        # 多模态时 content 是 [{type:text}, {type:image_url}]，原样透传
        return HumanMessage(content=content)

    if role == "assistant":
        tool_calls = []
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            tool_calls.append(
                {
                    "name": fn.get("name", ""),
                    "args": _loads(fn.get("arguments")),
                    "id": tc.get("id", ""),
                    "type": "tool_call",
                }
            )
        kwargs: dict[str, Any] = {}
        if m.get("reasoning_content") is not None:
            kwargs["reasoning_content"] = m["reasoning_content"]
        return AIMessage(
            content=content or "",
            tool_calls=tool_calls,
            additional_kwargs=kwargs,
        )

    if role == "tool":
        return ToolMessage(
            content=str(content),
            tool_call_id=m.get("tool_call_id", ""),
        )

    # 兜底：未知角色当作用户输入，避免整轮崩掉
    return HumanMessage(content=str(content))


def _loads(raw: Any) -> dict:
    import json

    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except Exception:                                        # noqa: BLE001
        return {}


def messages_to_dicts(messages: list[BaseMessage]) -> list[dict]:
    """反向转换（主要给测试与落库用）。"""
    out: list[dict] = []
    for m in messages:
        if isinstance(m, SystemMessage):
            out.append({"role": "system", "content": m.content})
        elif isinstance(m, HumanMessage):
            out.append({"role": "user", "content": m.content})
        elif isinstance(m, AIMessage):
            item: dict[str, Any] = {"role": "assistant", "content": m.content or ""}
            if m.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": tc.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": tc.get("name", ""),
                            "arguments": _dumps(tc.get("args") or {}),
                        },
                    }
                    for tc in m.tool_calls
                ]
            rc = m.additional_kwargs.get("reasoning_content")
            if rc is not None:
                item["reasoning_content"] = rc
            out.append(item)
        elif isinstance(m, ToolMessage):
            out.append(
                {"role": "tool", "tool_call_id": m.tool_call_id, "content": str(m.content)}
            )
    return out


def _dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)


# ============ 流式调用 ============


def content_text(message: Any) -> str:
    """把 chunk 的 content 归一化成纯文本。

    LangChain 1.x 的 content 可能是 str，也可能是 block 列表
    （``{"type":"text","text":...}``），两种都要能取到文本。
    """
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") in ("text", "text_delta"):
                    parts.append(str(block.get("text", "")))
        return "".join(parts)
    return ""


def reasoning_text(chunk: Any) -> str:
    """取 DeepSeek 推理模型的思维链增量。"""
    extra = getattr(chunk, "additional_kwargs", None) or {}
    value = extra.get("reasoning_content")
    if isinstance(value, str):
        return value
    # 1.x 标准 content block 里也可能出现
    content = getattr(chunk, "content", None)
    if isinstance(content, list):
        parts = [
            str(b.get("reasoning", ""))
            for b in content
            if isinstance(b, dict) and b.get("type") in ("reasoning", "thinking")
        ]
        return "".join(parts)
    return ""


def usage_from_message(message: Any) -> dict:
    """从聚合后的消息里取 token 用量与缓存命中。"""
    meta = getattr(message, "usage_metadata", None) or {}
    details = meta.get("input_token_details") or {}
    prompt = int(meta.get("input_tokens") or 0)
    completion = int(meta.get("output_tokens") or 0)
    total = int(meta.get("total_tokens") or (prompt + completion))

    # 各家字段不同：LangChain 归一到 input_token_details.cache_read
    hit = int(details.get("cache_read") or 0)
    if not hit:
        raw = (getattr(message, "response_metadata", None) or {}).get("token_usage") or {}
        hit = int(
            raw.get("prompt_cache_hit_tokens")
            or raw.get("cached_tokens")
            or (raw.get("prompt_tokens_details") or {}).get("cached_tokens")
            or 0
        )
    miss = int(details.get("cache_creation") or 0) or max(prompt - hit, 0)

    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cache_hit_tokens": hit,
        "cache_miss_tokens": miss,
    }


def aggregate(chunks) -> AIMessage:
    """把流式 chunk 聚合成一条完整 AIMessage（tool_calls 已解析成 dict）。"""
    total: AIMessageChunk | None = None
    for chunk in chunks:
        total = chunk if total is None else total + chunk
    if total is None:
        return AIMessage(content="")
    return AIMessage(
        content=total.content,
        tool_calls=list(getattr(total, "tool_calls", []) or []),
        additional_kwargs=dict(getattr(total, "additional_kwargs", {}) or {}),
        usage_metadata=getattr(total, "usage_metadata", None),
        response_metadata=dict(getattr(total, "response_metadata", {}) or {}),
    )
