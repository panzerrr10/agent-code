"""受限工具循环：给 Planner / Reviewer 这类辅助角色用的迷你 Agent。

## 为什么需要它

Planner 和 Reviewer 原来都是**纯文本调用**：Planner 不看代码就出计划，
Reviewer 读不到文件、跑不了测试，等于「LLM 给 LLM 盖章」。

这个模块给它们一个**受限的 ReAct 循环**：只允许白名单里的工具，
模型提到名单外的工具会被拒绝并告知，不会误执行。

## 重构说明

内部实现换成 LangChain（``bind_tools`` + ``invoke``），对外契约不变：
返回 ``{"text", "steps", "tool_calls", "denied", "error"}``，
所以 planner.py / reviewer.py 一行都不用改。

与主循环的区别（刻意保持简单）：
- 不流式（辅助角色的中间过程不需要逐字展示）
- 不做上下文压缩（步数上限很小，默认 6 步）
- 不做 pending / 续跑（失败了就降级，不影响主任务）
- **不弹确认**（没有交互通道）—— 白名单里只放只读工具，
  以及经过 policy 显式授权的 ``run_tests``
"""

from __future__ import annotations

import logging

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

import llm
import toolkit
from config_providers import get_provider
from tools import TOOLS_SCHEMA, execute_tool, tool_permission_kind

logger = logging.getLogger(__name__)

# 模型工厂：测试通过替换它注入假模型（等价于依赖注入点）
build_chat = llm.build_chat_model

# Planner 能用什么：**纯只读**。它需要的是「先看懂项目再规划」。
READONLY_TOOLS = (
    "list_dir",
    "read_file",
    "read_file_range",
    "search_files",
    "get_file_info",
    "list_symbols",
    "find_references",
    "get_function_body",
    "project_info",
)

# Reviewer 在此基础上多一个「跑测试」——验证结论的唯一硬证据。
TEST_TOOL = "run_tests"

DEFAULT_MAX_STEPS = 6


def _specs_for(allowed: set[str]) -> list[dict]:
    """从全量 schema 里筛出允许的工具（保持描述不变，模型才知道怎么用）。"""
    return [t for t in TOOLS_SCHEMA if t["function"]["name"] in allowed]


def _permission_decision(name: str) -> tuple[str, str]:
    """辅助角色能不能调用某个工具。返回 ``(action, message)``，action ∈ allow/deny。

    与主循环的区别：这里**没有 confirm 这一档**（没有 UI 通道去问用户），
    所以「需要确认」一律按「拒绝 + 说明」处理。
    """
    import runtime

    kind = tool_permission_kind(name)
    if kind in ("read", "internal"):
        return "allow", ""

    try:
        level = runtime.get_permission_level()
    except Exception:                                        # noqa: BLE001
        level = 1

    if level <= 1:
        return "deny", (
            f"⛔ 当前会话是【等级 1 · 只读模式】，`{name}` 不可用。"
            "请只依据已有信息给出结论。"
        )

    if name == TEST_TOOL:
        try:
            import policy

            if not policy.is_enabled("allow_reviewer_tests"):
                return "deny", (
                    "⛔ 评审阶段自动跑测试已被关闭（设置 → 执行策略）。"
                    "请只做静态审查，并在结论里说明「未运行测试」。"
                )
        except Exception:                                    # noqa: BLE001
            pass
        return "allow", ""

    return "deny", (
        f"⛔ `{name}` 属于 {kind} 档，辅助角色只允许只读工具，"
        "无法为你执行。请改用只读工具获取信息。"
    )


def run_tool_loop(
    *,
    system_prompt: str,
    user_content: str,
    allowed_tools,
    api_key: str = None,
    base_url: str = None,
    model: str = None,
    max_steps: int = DEFAULT_MAX_STEPS,
    extra_body: dict | None = None,
) -> dict:
    """跑一个受限的工具循环。

    Returns:
        ``{"text", "steps", "tool_calls", "denied", "error"}``

        - ``text``：模型最终的文本输出（失败时为空串）
        - ``tool_calls``：实际执行过的工具调用 ``[{"name","args"}]``，供 UI 展示
        - ``denied``：被拒绝的工具名（用于解释「为什么没跑测试」）
        - ``error``：出错说明；调用方据此降级，而不是抛给用户
    """
    result: dict = {"text": "", "steps": 0, "tool_calls": [], "denied": [], "error": None}

    allowed = {str(n) for n in (allowed_tools or ())}
    if not allowed:
        result["error"] = "没有可用工具"
        return result

    cfg = get_provider()
    try:
        chat = build_chat(
            api_key=api_key or cfg.get("api_key"),
            base_url=base_url or cfg.get("base_url"),
            model=model or cfg.get("default_model"),
            extra_body=extra_body,
            streaming=False,          # 辅助角色不需要逐字流式
        )
    except Exception as e:                                   # noqa: BLE001
        result["error"] = str(e)
        return result

    lc_tools = toolkit.build_tools(_specs_for(allowed))
    bound = chat.bind_tools(lc_tools) if lc_tools else chat

    messages: list = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_content),
    ]

    for step in range(max_steps):
        result["steps"] = step + 1
        try:
            ai = bound.invoke(messages)
        except Exception as e:                               # noqa: BLE001
            logger.warning("辅助角色 LLM 调用失败：%s", str(e)[:200])
            result["error"] = str(e)
            return result

        messages.append(ai)

        if not ai.tool_calls:
            result["text"] = llm.content_text(ai).strip()
            return result

        for tc in ai.tool_calls:
            name = tc.get("name", "")
            args = tc.get("args") or {}
            call_id = tc.get("id", "")

            action, deny_msg = _permission_decision(name)
            if action == "deny":
                result["denied"].append(name)
                messages.append(ToolMessage(content=deny_msg, tool_call_id=call_id))
                continue

            try:
                out = execute_tool(name, args)
            except Exception as e:                           # noqa: BLE001
                out = f"❌ 工具执行出错：{e}"
            result["tool_calls"].append({"name": name, "args": args})
            messages.append(ToolMessage(content=str(out), tool_call_id=call_id))

    result["error"] = f"超过最大步数 {max_steps}"
    return result
