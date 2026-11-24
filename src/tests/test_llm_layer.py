"""LangChain 层的单元测试（替代原来的 utils 重试测试）。

覆盖点：
- 消息 dict ↔ LangChain 消息的双向转换（reasoning_content 不能丢）
- 流式 chunk 的文本 / 思维链提取
- usage → token 与缓存统计的归一化
- 工具 schema 由 pydantic 生成（不再是手写 JSON）
"""

from langchain_core.messages import AIMessage, AIMessageChunk

import llm
import toolkit
from utils import cache_hit_rate, extract_json


class TestMessageConversion:
    def test_assistant_with_tool_calls_roundtrip(self):
        src = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                }
            ],
            "reasoning_content": "先看看文件",
        }
        msg = llm.dict_to_message(src)
        assert isinstance(msg, AIMessage)
        assert msg.tool_calls[0]["name"] == "read_file"
        assert msg.tool_calls[0]["args"] == {"path": "a.py"}
        assert msg.additional_kwargs["reasoning_content"] == "先看看文件"

        back = llm.messages_to_dicts([msg])[0]
        assert back["tool_calls"][0]["function"]["name"] == "read_file"
        assert back["reasoning_content"] == "先看看文件"

    def test_tool_message_keeps_call_id(self):
        msg = llm.dict_to_message({"role": "tool", "tool_call_id": "c9", "content": "结果"})
        assert msg.tool_call_id == "c9"
        assert msg.content == "结果"

    def test_multimodal_user_content_passthrough(self):
        content = [
            {"type": "text", "text": "看图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}},
        ]
        msg = llm.dict_to_message({"role": "user", "content": content})
        assert msg.content == content


class TestStreamParsing:
    def test_content_text_from_blocks(self):
        chunk = AIMessageChunk(content=[{"type": "text", "text": "你好"}])
        assert llm.content_text(chunk) == "你好"

    def test_content_text_from_str(self):
        assert llm.content_text(AIMessageChunk(content="abc")) == "abc"

    def test_reasoning_from_additional_kwargs(self):
        chunk = AIMessageChunk(content="", additional_kwargs={"reasoning_content": "思考"})
        assert llm.reasoning_text(chunk) == "思考"

    def test_aggregate_tool_calls(self):
        chunks = [
            AIMessageChunk(
                content="",
                tool_call_chunks=[
                    {"name": "read_file", "args": '{"path":', "id": "c1", "index": 0}
                ],
            ),
            AIMessageChunk(
                content="",
                tool_call_chunks=[{"name": None, "args": ' "a.py"}', "id": None, "index": 0}],
            ),
        ]
        msg = llm.aggregate(chunks)
        assert msg.tool_calls[0]["name"] == "read_file"
        assert msg.tool_calls[0]["args"] == {"path": "a.py"}


class TestUsage:
    def test_usage_from_metadata(self):
        msg = AIMessage(
            content="x",
            usage_metadata={
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
                "input_token_details": {"cache_read": 60},
            },
        )
        stats = llm.usage_from_message(msg)
        assert stats["prompt_tokens"] == 100
        assert stats["completion_tokens"] == 20
        assert stats["cache_hit_tokens"] == 60
        assert stats["cache_miss_tokens"] == 40

    def test_usage_missing_returns_zeros(self):
        stats = llm.usage_from_message(AIMessage(content="x"))
        assert stats["total_tokens"] == 0
        assert stats["cache_hit_tokens"] == 0


class TestToolkit:
    def test_builds_all_tools(self):
        tools = toolkit.build_tools()
        assert len(tools) == 29
        names = {t.name for t in tools}
        assert "read_file" in names and "run_command" in names

    def test_schema_from_pydantic(self):
        tools = {t.name: t for t in toolkit.build_tools()}
        schema = tools["read_file"].args_schema.model_json_schema()
        assert "path" in schema["properties"]
        assert schema["required"] == ["path"]

    def test_description_is_preserved(self):
        import tools as tools_mod

        expected = {t["function"]["name"]: t["function"]["description"] for t in tools_mod.TOOLS_SCHEMA}
        for tool in toolkit.build_tools():
            assert tool.description == expected[tool.name]


class TestHelpers:
    def test_extract_json_with_fence(self):
        assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_extract_json_with_noise(self):
        assert extract_json('好的：{"a": 2} 以上') == {"a": 2}

    def test_cache_hit_rate_uses_prompt_tokens(self):
        assert cache_hit_rate(50, 100) == 0.5
        assert cache_hit_rate(0, 0) is None
