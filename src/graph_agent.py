"""编排层：用 LangGraph 的 ``StateGraph`` 表达 Agent 循环。

## 为什么用图，而不是继续手写 for 循环

原来的循环是「一个 1074 行的函数里，用 if/else + continue 表达状态转移」。
换成图之后，三个状态被显式建模：

    model ──(有 tool_calls)──> tools ──> model
      └────(无 tool_calls)────> END
                  └──(需要人工确认)──> END（挂起，等前端确认后从断点重入）

好处不是「代码变少」（其实差不多），而是：

- **控制流可读**：一眼能看出「工具执行完必然回到模型」，不用在 1000 行里找 continue
- **可扩展**：以后加「Reviewer 不通过 → 回 Executor 重试」只需要加一条条件边
- **可观测**：每个节点是一次独立的 step，接 tracing 时天然有边界

## 刻意保留的两个自研点

1. **权限闸门**仍在节点里显式调用 ``_permission_gate`` —— 不用 LangChain 的
   middleware 包，因为安全判定必须是「一处、可审计、服务端权威」。
2. **消息格式仍是 OpenAI dict**（不是 LangChain Message）—— 上下文压缩、
   断点续跑、落库格式全建立在这个结构上，换成对象会牵动 582 行压缩逻辑。
   模型调用前才临时转成 LangChain 消息（见 ``llm.dict_to_message``）。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

import llm
from config import CONTEXT_BUDGET_TOKENS, CONTEXT_KEEP_RECENT_TOOL_RESULTS

logger = logging.getLogger(__name__)

STREAM_FLUSH_SEC = 0.05


class RunCancelled(Exception):
    """用户点了停止。由节点抛出，外层捕获后保存断点。

    ``messages`` 用来带出**已裁剪**的对话（去掉尚未执行的 tool_calls），
    否则断点里会留下悬空调用，续跑时 LLM 直接 400。
    """

    def __init__(self, messages: list[dict] | None = None, step: int = 0) -> None:
        super().__init__("cancelled")
        self.messages = messages
        self.step = step


class AgentState(TypedDict, total=False):
    """图的共享状态。"""

    messages: list[dict]      # OpenAI 格式的完整对话
    step: int                 # 已执行的步数
    usage: dict               # 累计 token 用量
    pending: dict | None      # 挂起等待确认的工具调用
    confirm_token: str | None
    finished: bool            # 是否已产出最终回答
    failures: dict            # 工具连续失败计数（熔断用）
    stop: bool                # 是否因熔断而终止


class GraphContext:
    """一次运行所需的全部依赖（图按运行构建，天然隔离会话）。"""

    def __init__(
        self,
        *,
        model,
        tools: list,
        tool_map: dict[str, Any],
        session_id: int | None,
        run_index: int | None,
        auto_confirm: bool,
        max_steps: int,
        persist: Callable[[list[dict], int], None],
        save_pending: Callable[[list[dict], int, dict, str], str],
        gate: Callable[[str, dict, bool], tuple[str, str]],
        confirm_event: Callable[[str, dict, str], dict],
        budget_tokens: int = CONTEXT_BUDGET_TOKENS,
        keep_recent: int = CONTEXT_KEEP_RECENT_TOOL_RESULTS,
        execute: Callable[[str, dict], str] | None = None,
    ) -> None:
        self.model = model
        self.tools = tools
        self.tool_map = tool_map
        self.session_id = session_id
        self.run_index = run_index
        self.auto_confirm = auto_confirm
        self.max_steps = max_steps
        self.persist = persist
        self.save_pending = save_pending
        self.gate = gate
        self.confirm_event = confirm_event
        # 预算与工具执行都从调用方注入：既让测试能替换，也让图本身不依赖全局配置
        self.budget_tokens = budget_tokens
        self.keep_recent = keep_recent
        self.execute = execute


def _emit(event: dict) -> None:
    """往 SSE 流里写一条事件。不在图执行上下文中时静默丢弃。"""
    try:
        get_stream_writer()(event)
    except Exception:                                        # noqa: BLE001
        pass


def _is_cancelled() -> bool:
    try:
        import runtime

        return runtime.is_cancelled()
    except Exception:                                        # noqa: BLE001
        return False


# ============ 节点 ============


def _model_node(ctx: GraphContext):
    def run(state: AgentState) -> dict:
        if _is_cancelled():
            raise RunCancelled()

        import compaction

        messages = list(state.get("messages") or [])
        step = int(state.get("step") or 0)

        # 上下文压缩：必须在两步之间的安全边界做（此处没有悬空 tool_call）
        overhead = len(json.dumps(
            [t.args_schema.model_json_schema() for t in ctx.tools]
        )) // compaction.CHAR_PER_TOKEN if ctx.tools else 0
        stats = compaction.compact(
            messages,
            budget_tokens=ctx.budget_tokens,
            overhead_tokens=overhead,
            keep_recent=ctx.keep_recent,
        )
        # 只在**真的压掉了东西**时上报：否则每一步都发一条「0 压缩」的噪声事件，
        # 用户会以为压缩一直在生效。压不下去的情况由下面的 over_budget 警告表达。
        if stats["after_tokens"] < stats["before_tokens"]:
            _emit({"type": "context_stats", **stats})
        if not stats["pairing_ok"]:
            compaction.repair_pairing(messages)
            if compaction.check_pairing(messages):
                raise RuntimeError("上下文压缩破坏了工具调用配对，已中止本次运行")

        # 断点：必须在 LLM 调用之前（此处的 messages 是安全边界）
        ctx.persist(messages, step)

        # 模型调用（流式）
        lc_messages = [llm.dict_to_message(m) for m in messages]
        bound = ctx.model.bind_tools(ctx.tools) if ctx.tools else ctx.model

        buffer = ""
        reasoning = ""
        pending_text = ""
        last_flush = time.monotonic()

        stream = bound.stream(lc_messages)
        chunks = []
        for chunk in stream:
            if _is_cancelled():
                try:
                    stream.close()
                except Exception:                            # noqa: BLE001
                    pass
                raise RunCancelled()

            chunks.append(chunk)

            delta = llm.content_text(chunk)
            if delta:
                buffer += delta
                pending_text += delta
                now = time.monotonic()
                if now - last_flush >= STREAM_FLUSH_SEC:
                    _emit({"type": "text", "content": pending_text})
                    pending_text = ""
                    last_flush = now

            rc = llm.reasoning_text(chunk)
            if rc:
                reasoning += rc

        if pending_text:
            _emit({"type": "text", "content": pending_text})

        message = llm.aggregate(chunks)
        usage = llm.usage_from_message(message)

        assistant: dict[str, Any] = {
            "role": "assistant",
            "content": buffer or llm.content_text(message) or "",
            "tool_calls": [
                {
                    "id": tc.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": tc.get("name", ""),
                        "arguments": json.dumps(tc.get("args") or {}, ensure_ascii=False),
                    },
                }
                for tc in (message.tool_calls or [])
            ],
        }
        if not assistant["tool_calls"]:
            assistant.pop("tool_calls")
        if reasoning:
            assistant["reasoning_content"] = reasoning

        total = dict(state.get("usage") or {})
        for key in ("prompt_tokens", "completion_tokens", "total_tokens",
                    "cache_hit_tokens", "cache_miss_tokens"):
            total[key] = int(total.get(key, 0)) + int(usage.get(key, 0))

        return {
            "messages": messages + [assistant],
            "step": step + 1,
            "usage": total,
            "finished": not assistant.get("tool_calls"),
        }

    return run


def _trim_unprocessed(messages: list[dict], current_ids: list[str], processed: list[str]) -> list[dict]:
    """裁掉「还没执行」的 tool_calls，避免续跑时出现悬空调用（会 400）。"""
    keep = set(processed)
    current = set(current_ids)
    out: list[dict] = []
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            tcs = [t for t in m["tool_calls"] if t["id"] not in current or t["id"] in keep]
            if not tcs:
                continue
            out.append({**m, "tool_calls": tcs})
        else:
            out.append(m)
    return out


def _tools_node(ctx: GraphContext):
    def run(state: AgentState) -> dict:
        messages = list(state.get("messages") or [])
        step = int(state.get("step") or 0)

        # 续跑：先执行上次挂起的那个工具
        pending = state.get("pending")
        if pending:
            result = _execute(ctx, pending["name"], pending.get("args") or {})
            _emit({"type": "tool_result", "content": result})
            messages.append(
                {"role": "tool", "tool_call_id": pending.get("id", ""), "content": result}
            )
            return {"messages": messages, "pending": None, "confirm_token": None}

        last = messages[-1] if messages else {}
        tool_calls = last.get("tool_calls") or []
        all_ids = [tc.get("id", "") for tc in tool_calls]
        processed: list[str] = []
        failures = dict(state.get("failures") or {})

        for tc in tool_calls:
            if _is_cancelled():
                raise RunCancelled(_trim_unprocessed(messages, all_ids, processed), step)

            fn = tc.get("function") or {}
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:                                # noqa: BLE001
                args = {}

            _emit({"type": "tool_call", "name": name, "args": args})

            action, deny_msg = ctx.gate(name, args, ctx.auto_confirm)

            if action == "deny":
                _emit({"type": "permission_denied", "name": name, "content": deny_msg})
                messages.append(
                    {"role": "tool", "tool_call_id": tc.get("id", ""), "content": deny_msg}
                )
                processed.append(tc.get("id", ""))
                continue

            if action == "confirm":
                # step 是「已经跑完的模型调用次数」，而挂起点属于**产生这次工具调用**
                # 的那一步（0 基），所以减一 —— 续跑时前端显示的步数才对得上。
                token = ctx.save_pending(
                    _trim_unprocessed(messages, all_ids, processed),
                    max(step - 1, 0),
                    {"id": tc.get("id", ""), "name": name, "args": args},
                    "",
                )
                _emit(ctx.confirm_event(name, args, token))
                return {
                    "messages": messages,
                    "pending": {"id": tc.get("id", ""), "name": name, "args": args},
                    "confirm_token": token,
                }

            result = _execute(ctx, name, args)

            # 连续失败熔断：会改磁盘 / 会执行的工具连错 3 次就停手，
            # 避免它在同一个坑里反复重试烧 token。**成功必须清零** ——
            # 否则「失败→成功→失败→成功→失败」也会被误判成「连续失败」。
            failed = isinstance(result, str) and result.startswith("❌")
            if failed:
                failures[name] = failures.get(name, 0) + 1
            else:
                failures[name] = 0

            content = result
            if failed and _is_privileged(name):
                count = failures[name]
                if count >= 3:
                    _emit({"type": "tool_result", "content": result})
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.get("id", ""), "content": result}
                    )
                    _emit(
                        {
                            "type": "text",
                            "content": (
                                f"\n\n⚠️ 工具 `{name}` 已连续失败 3 次，"
                                "停止执行。请检查任务描述或手动处理。"
                            ),
                        }
                    )
                    return {
                        "messages": messages,
                        "failures": failures,
                        "stop": True,
                        "finished": True,
                    }
                if count >= 2:
                    # 警告并进 tool 结果，**不要**插成独立 user 消息 ——
                    # 那会造成 assistant→user→tool 的非法交错，压缩自检直接失败。
                    content = (
                        f"{result}\n\n⚠️ 系统提示：`{name}` 已连续失败 {count} 次。"
                        "请分析失败原因，换个方式重试，或告诉用户无法完成。"
                    )

            _emit({"type": "tool_result", "content": content})
            messages.append(
                {"role": "tool", "tool_call_id": tc.get("id", ""), "content": content}
            )
            processed.append(tc.get("id", ""))

            if name in ("create_plan", "update_plan", "get_plan"):
                try:
                    import tools as tools_mod

                    _emit({"type": "plan_update", "plan": tools_mod.get_plan_data()})
                except Exception:                            # noqa: BLE001
                    pass

        return {"messages": messages, "failures": failures}

    return run


def _execute(ctx: GraphContext, name: str, args: dict) -> str:
    """执行工具（优先走注入的执行入口，其次用 LangChain 工具对象）。"""
    import tools as tools_mod

    if ctx.execute is not None:
        try:
            out = ctx.execute(name, args)
            return out if isinstance(out, str) else str(out)
        except Exception as e:                               # noqa: BLE001
            return f"❌ 工具执行出错：{e}"

    tool = ctx.tool_map.get(name)
    if tool is not None:
        try:
            out = tool.invoke(args)
            return out if isinstance(out, str) else str(out)
        except Exception as e:                               # noqa: BLE001
            return f"❌ 工具执行出错：{e}"
    return tools_mod.execute_tool(name, args)


# ============ 路由 ============


def _route_entry(state: AgentState) -> str:
    """入口路由：续跑时先执行挂起的那个工具，否则从模型开始。"""
    return "tools" if state.get("pending") else "model"


def _route_after_model(state: AgentState) -> str:
    messages = state.get("messages") or []
    last = messages[-1] if messages else {}
    if last.get("tool_calls"):
        return "tools"
    return END


def _is_privileged(name: str) -> bool:
    try:
        import tools

        return tools.is_privileged_tool(name)
    except Exception:                                        # noqa: BLE001
        return False


def _route_after_tools(state: AgentState) -> str:
    if state.get("stop"):
        return END
    if state.get("pending"):
        return END
    if int(state.get("step") or 0) >= 10_000:                # 兜底，实际由 recursion_limit 控制
        return END
    return "model"


# ============ 构建 ============


def build_graph(ctx: GraphContext):
    """构建并编译一次运行的图。"""
    graph = StateGraph(AgentState)
    graph.add_node("model", _model_node(ctx))
    graph.add_node("tools", _tools_node(ctx))
    graph.add_conditional_edges(
        START, _route_entry, {"tools": "tools", "model": "model"}
    )
    graph.add_conditional_edges("model", _route_after_model, {"tools": "tools", END: END})
    graph.add_conditional_edges("tools", _route_after_tools, {"model": "model", END: END})
    return graph.compile()


def run_graph(ctx: GraphContext, state: AgentState, max_steps: int):
    """执行图并逐条产出事件。

    用 ``stream_mode=["custom","values"]``：
    - ``custom`` 是节点里 ``_emit`` 写出的 SSE 事件（逐条实时）
    - ``values`` 是每步之后的完整状态（用于拿最终 usage / pending）

    Yields:
        ``(kind, payload)``：kind 为 ``"event"`` 或 ``"state"``。
    """
    app = build_graph(ctx)
    config = {"recursion_limit": max(2, max_steps * 2 + 2)}
    for mode, data in app.stream(state, config=config, stream_mode=["custom", "values"]):
        if mode == "custom":
            yield "event", data
        else:
            yield "state", data
