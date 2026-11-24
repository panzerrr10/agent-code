"""工具层：把现有的 29 个工具包装成 LangChain ``StructuredTool``。

## 做法

工具实现（读写文件、沙箱执行、git、计划、RAG）**完全复用** —— 它们已经过
800 多个测试和真实任务验证，重写没有收益。这里只做两件事：

1. 把 ``TOOLS_SCHEMA`` 里的 JSON Schema 转成 pydantic 模型，交给
   ``StructuredTool``。之后 ``bind_tools`` 会自动生成给模型看的 schema，
   不再手写 450 行 JSON。
2. 保持 **description 原样**。这些描述是给模型看的提示词（例如「不要写
   ``cd /workspace``」），自动生成的措辞会丢信息，所以逐字保留。

## 权限闸门不在这里

工具**能不能执行**由 ``agent._permission_gate`` 决定（全项目唯一判定点）。
本模块只负责「怎么调」，不负责「准不准调」—— 安全边界不能散落在工具层。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, create_model

logger = logging.getLogger(__name__)

# JSON Schema 类型 → Python 类型
_TYPE_MAP: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _args_model(name: str, parameters: dict) -> type[BaseModel]:
    """从 JSON Schema 生成 pydantic 参数模型。

    ``required`` 里的字段无默认值；其余给 ``None`` 默认（模型经常省略可选参数，
    缺省会直接抛 ValidationError，工具就白调了）。
    """
    properties = (parameters or {}).get("properties") or {}
    required = set((parameters or {}).get("required") or [])

    fields: dict[str, tuple[Any, Any]] = {}
    for pname, spec in properties.items():
        py_type = _TYPE_MAP.get(spec.get("type", "string"), Any)
        desc = spec.get("description", "")
        if pname in required:
            fields[pname] = (py_type, Field(..., description=desc))
        else:
            default = spec.get("default")
            fields[pname] = (py_type | None, Field(default, description=desc))

    return create_model(f"{name}_Args", **fields)


def _impl_for(name: str) -> Callable[..., Any]:
    """取工具实现：内置工具走 TOOL_FUNCTIONS，MCP 工具走统一派发入口。"""
    import tools

    if name in tools.TOOL_FUNCTIONS:
        return tools.TOOL_FUNCTIONS[name]

    # MCP 插件工具：名字形如 mcp__server__tool，由 execute_tool 统一派发
    def _mcp(**kwargs):
        return tools.execute_tool(name, kwargs)

    return _mcp


def build_tool(spec: dict) -> StructuredTool:
    """把一个 ``{"type":"function","function":{...}}`` 规格转成 LangChain 工具。"""
    fn = spec.get("function") or spec
    name = fn["name"]
    return StructuredTool.from_function(
        func=_impl_for(name),
        name=name,
        description=fn.get("description", ""),
        args_schema=_args_model(name, fn.get("parameters") or {}),
    )


def build_tools(specs: list[dict] | None = None) -> list[StructuredTool]:
    """批量构建。

    Args:
        specs: 工具规格列表；为空时用全部内置工具。

    Returns:
        LangChain 工具列表，可直接 ``model.bind_tools(tools)``。
    """
    import tools

    specs = specs if specs is not None else tools.TOOLS_SCHEMA
    out: list[StructuredTool] = []
    for spec in specs:
        try:
            out.append(build_tool(spec))
        except Exception as e:                               # noqa: BLE001
            logger.warning("工具 %s 构建失败：%s", (spec.get("function") or {}).get("name"), e)
    return out


def tool_map(specs: list[dict] | None = None) -> dict[str, StructuredTool]:
    """名字 → 工具，供图里的执行节点按 tool_call 查表。"""
    return {t.name: t for t in build_tools(specs)}


def selected_specs(task: str, full: bool = False) -> list[dict]:
    """按任务挑工具（复用原有的关键词选择逻辑，含 MCP 按需注入）。"""
    import tools

    return tools.get_tools_schema(task=task, full=full)
