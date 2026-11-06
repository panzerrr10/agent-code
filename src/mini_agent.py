"""受限工具循环：给「辅助角色」用的迷你 Agent。

## 为什么需要它

Planner 和 Reviewer 原来是**纯文本模型调用**：

- **Planner 没有任何工具、不读代码** —— 它在完全没看过项目的情况下先出一份计划，
  然后 executor 被硬性要求「严格按此计划执行，不要重新 create_plan」。
  对一个真实代码库，盲订的计划经常是错的，而系统还强制你按错的走。
- **Reviewer 不能读文件、不能跑测试** —— 它的输入只有 executor **自述的最终文本**
  加一段工具调用摘要。换句话说，它是「LLM 给 LLM 盖章」，**无法验证任何东西**。

这个模块提供一个**受限的 ReAct 循环**：给一组白名单工具，让辅助角色自己去查证。
允许调用的工具是硬白名单 —— 模型提到名单外的工具会被直接拒绝并告知，不会误执行。

## 与主循环的区别（刻意保持简单）

- 不流式（辅助角色的中间过程不需要逐字展示）
- 不做上下文压缩（步数上限很小，默认 6 步）
- 不做 pending / 续跑（辅助角色失败了就降级，不影响主任务）
- **不弹确认**（没有交互通道）—— 所以白名单里只放只读工具，
  以及经过 policy 显式授权的 ``run_tests``
"""

from __future__ import annotations

import json
import logging

from config_providers import get_provider
from tools import execute_tool, get_tools_schema, tool_permission_kind
from utils import call_llm_with_retry, get_llm_client

logger = logging.getLogger(__name__)

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


def _schema_for(allowed: set[str]) -> list:
    """从全量 schema 里筛出允许的工具（保持 schema 描述不变，模型才知道怎么用）。"""
    return [t for t in get_tools_schema(full=True) if t["function"]["name"] in allowed]


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
        import policy

        if policy.is_enabled("allow_reviewer_tests"):
            return "allow", ""
        return "deny", (
            f"⛔ `{name}` 已被策略关闭（设置 → 执行策略 → 评审阶段自动跑测试）。"
            "请只做静态审查，并在结论里说明「未运行测试」。"
        )

    return "deny", (
        f"⛔ `{name}` 属于 {kind} 档，辅助角色只允许只读工具，"
        "无法为你执行。请改用只读工具获取信息。"
    )


def _client_and_model(api_key=None, base_url=None, model=None):
    """按传入覆盖 / 默认 provider 取客户端与模型名（复用 utils 的收敛实现）。"""
    cfg = get_provider()
    if api_key:
        client = get_llm_client(api_key, base_url or cfg["base_url"])
    else:
        client = get_llm_client(cfg["api_key"], cfg["base_url"])
    return client, (model or cfg["default_model"])


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
    """跑一个受限的工具循环，返回：

    ``{"text", "steps", "tool_calls", "denied", "error"}``

    - ``text``     模型最终的文本输出（失败时为空串）
    - ``tool_calls`` 实际执行过的工具调用记录 ``[{"name","args"}]``，供 UI 展示
    - ``denied``   被拒绝的工具调用名（用于给用户解释「为什么没跑测试」）
    - ``error``    出错时的说明；调用方应当据此降级，而不是抛给用户
    """
    result = {"text": "", "steps": 0, "tool_calls": [], "denied": [], "error": None}

    allowed = {str(n) for n in (allowed_tools or ())}
    if not allowed:
        result["error"] = "没有可用工具"
        return result

    try:
        client, actual_model = _client_and_model(api_key, base_url, model)
    except Exception as e:                                   # noqa: BLE001
        result["error"] = str(e)
        return result

    tools = _schema_for(allowed)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    for step in range(max_steps):
        result["steps"] = step + 1
        kwargs = dict(
            model=actual_model,
            messages=messages,
            temperature=0,
        )
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if extra_body:
            kwargs["extra_body"] = extra_body

        try:
            resp = call_llm_with_retry(client, **kwargs)
        except Exception as e:                               # noqa: BLE001
            logger.warning("辅助角色 LLM 调用失败：%s", str(e)[:200])
            result["error"] = str(e)
            return result

        if not resp.choices:
            result["error"] = "模型没有返回内容"
            return result
        msg = resp.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None)

        if not tool_calls:
            result["text"] = (msg.content or "").strip()
            return result

        # 把 assistant 消息（含 tool_calls）放进历史，否则 tool 回复会悬空
        messages.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments or "{}",
                        },
                    }
                    for tc in tool_calls
                ],
            }
        )

        for tc in tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except Exception:                                # noqa: BLE001
                args = {}
            if not isinstance(args, dict):
                args = {}

            if name not in allowed:
                content = (
                    f"⛔ 工具 `{name}` 不在本角色的可用列表里，已拒绝。"
                    f"可用工具：{', '.join(sorted(allowed))}"
                )
                result["denied"].append(name)
            else:
                action, reason = _permission_decision(name)
                if action == "deny":
                    content = reason
                    result["denied"].append(name)
                else:
                    try:
                        content = execute_tool(name, args)
                        result["tool_calls"].append({"name": name, "args": args})
                    except Exception as e:                   # noqa: BLE001
                        content = f"❌ 工具执行失败：{e}"

            messages.append(
                {"role": "tool", "tool_call_id": tc.id, "content": str(content)[:8000]}
            )

    # 步数用尽：把最后一次的文本（如果有）当结论，并明确标注
    result["error"] = f"达到最大步数 {max_steps}"
    last = messages[-1] if messages else {}
    if isinstance(last, dict) and last.get("role") == "assistant":
        result["text"] = (last.get("content") or "").strip()
    return result
