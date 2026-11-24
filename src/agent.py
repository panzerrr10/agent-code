"""Agent 入口：组装上下文 → 交给 LangGraph 执行 → 逐条产出 SSE 事件。

## 本文件在重构后的职责

以前这里是 **1074 行**：上下文组装 + ReAct 循环 + 流式解析 + 权限判定 +
断点保存 + 错误分类全在一个函数里。现在循环搬进了 ``graph_agent``，
本文件只留三件事：

1. **组装初始状态**：系统提示词、长期记忆、Planner 计划、AGENTS.md、
   代码库索引、历史消息、附件与图片
2. **提供回调**：权限闸门、断点保存、确认事件（图里通过 ``GraphContext`` 注入）
3. **翻译结果**：把图产出的事件/状态翻译成前端认的 SSE 契约

这样「怎么循环」和「循环里做什么」就分开了。
"""

from __future__ import annotations

import copy
import json
import secrets

import compaction  # noqa: F401  （测试与排障会直接读 agent.compaction）
import project_context
import repo_context
from config import CONTEXT_BUDGET_TOKENS, CONTEXT_KEEP_RECENT_TOOL_RESULTS, MAX_STEPS  # noqa: F401
from config_providers import (
    get_default_provider_id,
    get_extra_body,
    get_provider,
    get_provider_id_by_base_url,
)
from graph_agent import STREAM_FLUSH_SEC, GraphContext, RunCancelled, run_graph  # noqa: F401
from tools import execute_tool, is_privileged_tool, is_safe_command
from utils import cache_hit_rate

SYSTEM_PROMPT = """你是 AI Agent，能用工具完成用户任务。

## 工具
- 读：list_dir / read_file / search_files / get_file_info
- 写：create_file / write_file（自动弹勾叉）
- 删：delete_file（自动弹勾叉）
- 执行：run_python / run_command（Docker 沙箱）
- 规划：create_plan / update_plan / get_plan
- 知识库：search_knowledge（用户上传的文档）
- 记忆：save_memory / list_memories / delete_memory
- 编辑：edit_file（精确替换文件内容）
- 读取：read_file_range（读文件指定行）

## 原则
1. **先探索再行动**：对任何你本次会话中还没 read_file 过的文件，不要猜测它的内容，必须先 read_file。
2. **基于事实**：只用工具返回的内容回答，不编造。
3. **简洁回答**：用 Markdown，清楚、有条理。
4. **危险操作**：delete_file / write_file / run_command / run_python / git_commit / git_rollback 由系统自动弹勾叉，你直接调用即可。
5. **复杂任务先规划**：3 步以上先 create_plan，逐步 update_plan(doing/done)。
6. **有 Planner 计划时**：按计划执行，不要重新 create_plan。
7. **知识库优先**：见下方「知识库规范」章节。
8. **长期记忆**：说「记住 X」→ save_memory；问「你记得什么」→ list_memories；说「忘掉 X」→ delete_memory。
9. **改代码一律用 edit_file**：read_file（≤200 行）或 read_file_range（>200 行，分段读）看内容，然后 edit_file 精确替换。只在创建新文件时用 create_file。**禁止用 run_command 改代码**。
10. **工具失败时**：第一次失败 → 检查参数后重试 1 次；仍失败 → 换一种方法尝试；两次不同方法都失败 → 报告"无法完成"给用户。
11. **测试驱动**：改完代码后必须用 `run_tests` 验证；最多重试 3 次；3 次仍失败就报告给用户。
12. **先说明再做（强制）**：在调用 write_file / delete_file / run_command 这些会弹确认的工具之前，**必须先输出一句人类可读的说明**。

## 命令执行规范（重要）
- 执行 run_command 时，**不要写 `cd /workspace`**，系统会自动切换工作目录
- **不要用 `&&`、`|`、`;`、`>` 连接多个命令**，这些会触发安全拦截
- 如果命令需要输出重定向，用 run_python 执行 Python 代码代替

## 知识库规范
- **何时用**：用户提到"上传的文档" / "知识库" / "之前传的文件"，或问题可能与上传文档有关时，必须用 search_knowledge。
- **怎么搜**：query 用核心关键词。找不到就换关键词重试，最多试 2 次。
- **怎么用结果**：严格基于返回内容回答。返回"未找到"就告诉用户"知识库中没有这方面的信息"。

## 路径
- 工作区是 `/workspace/`（= 用户选定的项目根目录）。
- 也可以用相对路径，默认在 `/workspace/` 下。
- 敏感目录（`.ssh`、`.config`、`.cache` 等）禁止访问。
"""

PERM_READONLY, PERM_CONFIRM, PERM_FULL = 1, 2, 3
ATTACH_PREVIEW_CHARS = 6000


def _ensure_reasoning_consistency(messages: list[dict]) -> None:
    """DeepSeek 推理模型：只要有一条 assistant 带 reasoning_content，
    则所有 assistant 都必须带（值可为空串），否则续跑直接 400。"""
    has_any = any(
        m.get("role") == "assistant" and "reasoning_content" in m for m in messages
    )
    if not has_any:
        return
    for m in messages:
        if m.get("role") == "assistant" and "reasoning_content" not in m:
            m["reasoning_content"] = ""


def _format_attachments(attachments, preview_chars: int = ATTACH_PREVIEW_CHARS) -> str:
    """把附件正文拼进用户消息：预览内联 + 完整落盘路径。"""
    blocks = []
    for i, att in enumerate(attachments or [], 1):
        if not isinstance(att, dict):
            continue
        name = att.get("name") or "附件"
        path = att.get("path") or ""
        text = att.get("text") or ""
        total = len(text)
        block = (
            f"【附件 {i}】{name}（共 {total} 字符）\n"
            f"已保存到：{path}\n"
            f"----- 内容开始 -----\n{text[:preview_chars]}\n----- 内容结束 -----"
        )
        if total > preview_chars:
            block += (
                f"\n（上面只显示了前 {preview_chars} 字符。"
                f"需要完整内容请用 read_file 读 `{path}`；需要修改就直接 edit_file 这个路径。）"
            )
        blocks.append(block)
    if not blocks:
        return ""
    return "\n\n" + "\n\n".join(blocks)


def _permission_gate(func_name: str, args: dict, auto_confirm: bool):
    """权限闸门：**所有工具执行前都必须过这里**，返回 (action, message)。"""
    import runtime

    if not is_privileged_tool(func_name):
        return "allow", ""

    level = PERM_FULL if auto_confirm else runtime.get_permission_level()

    if level == PERM_READONLY:
        return "deny", (
            f"⛔ 当前会话是【等级 1 · 只读模式】，已拒绝执行 `{func_name}`。"
            "你只能读取和分析文件，不能修改或执行命令；"
            "请把具体的修改建议告诉用户，由用户自己操作。"
        )

    if level == PERM_CONFIRM and func_name == "run_command":
        if is_safe_command(args.get("cmd", "") or ""):
            return "allow", ""

    if level == PERM_FULL:
        return "allow", ""
    return "confirm", ""


def _confirm_event(func_name: str, args: dict, token: str) -> dict:
    """按工具类型构造确认卡片事件。"""
    import os as _os

    if isinstance(func_name, str) and func_name.startswith("mcp__"):
        import mcp_client

        entry = mcp_client.resolve(func_name)
        server = entry["server"] if entry else "?"
        tool = entry["name"] if entry else func_name
        try:
            pretty = json.dumps(args or {}, ensure_ascii=False, indent=2)
        except Exception:                                    # noqa: BLE001
            pretty = str(args)
        return {
            "type": "confirm_command",
            "cmd": f"[MCP 插件] {server} → {tool}\n参数：\n{pretty[:600]}",
            "confirm_token": token,
        }

    if func_name == "delete_file":
        return {"type": "confirm_delete", "path": args.get("path", ""), "confirm_token": token}

    file_labels = {
        "write_file": "覆盖写入",
        "create_file": "创建文件",
        "edit_file": "编辑文件",
        "multi_edit": "批量编辑",
    }
    if func_name in file_labels:
        path = args.get("path", "")
        old_content = args.get("old_text", "") or ""
        new_content = args.get("new_text", "") or args.get("content", "") or ""

        if func_name == "multi_edit":
            edits = args.get("edits") or []
            paths = sorted({str(e.get("path", "")) for e in edits if isinstance(e, dict)})
            path = "、".join(p for p in paths if p) or "（多个文件）"
            old_content = "\n\n".join(
                str(e.get("old_text", "")) for e in edits if isinstance(e, dict)
            )[:2000]
            new_content = "\n\n".join(
                str(e.get("new_text", "")) for e in edits if isinstance(e, dict)
            )[:2000]
        elif not old_content:
            try:
                if path and _os.path.isfile(path):
                    with open(path, encoding="utf-8", errors="ignore") as f:
                        old_content = f.read()
            except Exception:                                # noqa: BLE001
                old_content = "（无法读取原内容）"

        return {
            "type": "confirm_write",
            "path": path,
            "old_content": old_content,
            "new_content": new_content,
            "label": file_labels[func_name],
            "confirm_token": token,
        }

    if func_name == "run_python":
        cmd = "# run_python 执行代码：\n" + (args.get("code") or "")[:300]
    elif func_name == "run_command":
        cmd = args.get("cmd", "")
    elif func_name == "git_commit":
        cmd = f"git commit: {args.get('message', '')}"
    elif func_name == "git_rollback":
        cmd = f"git rollback: {args}"
    elif func_name == "run_tests":
        cmd = f"run_tests: {args.get('path') or '(自动识别测试框架)'}"
    else:
        cmd = f"{func_name} {args}"
    return {"type": "confirm_command", "cmd": cmd, "confirm_token": token}


def _build_model(provider_id: str | None, user_api_key, user_base_url, user_model):
    """按 provider 配置构造 LangChain 聊天模型。"""
    import llm

    default_id = get_default_provider_id()
    cfg = get_provider(provider_id or default_id)
    api_key = user_api_key or cfg.get("api_key")
    base_url = user_base_url or cfg.get("base_url")
    model_name = user_model or cfg.get("default_model")

    if not api_key:
        raise RuntimeError(
            f"默认提供商「{cfg.get('name') or '（未配置）'}」还没有 api_key，"
            "请在 设置 → 模型与密钥 里添加"
        )

    effective_id = provider_id or get_provider_id_by_base_url(base_url) or default_id
    return llm.build_chat_model(
        api_key=api_key,
        base_url=base_url,
        model=model_name,
        extra_body=get_extra_body(effective_id),
    )


def run_agent_stream(
    user_task: str,
    session_id: int = None,
    provider_id: str = None,
    max_steps: int = MAX_STEPS,
    auto_confirm: bool = False,
    planner_plan: list = None,
    user_api_key: str = None,
    user_base_url: str = None,
    user_model: str = None,
    project_root: str = None,
    rag_api_key: str = None,
    rag_base_url: str = None,
    images: list = None,
    attachments: list = None,
    _resume: bool = False,
):
    """Agent 主入口：流式 yield 事件（供 Flask SSE 用）。

    Yields:
        dict：``{"type": "text"|"tool_call"|"tool_result"|"done"|"error"|...}``
    """
    import runtime

    runtime.set_session_id(session_id)
    runtime.register_run(session_id)

    import db

    memories = db.get_all_memories()
    memory_context = ""
    if memories:
        lines = ["\n\n## 你的长期记忆（关于用户的偏好和重要信息）"]
        lines += [f"- {m['content']}" for m in memories]
        memory_context = "\n".join(lines)

    plan_context = ""
    if planner_plan:
        lines = ["\n\n## 已由 Planner 生成的计划（请严格按此执行）"]
        lines += [f"{i}. {s}" for i, s in enumerate(planner_plan, 1)]
        lines.append("\n**请按此计划执行，不要重新调用 create_plan**。")
        plan_context = "\n".join(lines)

    if user_api_key:
        runtime.set_api_key(user_api_key)
    if user_base_url:
        runtime.set_base_url(user_base_url)
    if rag_api_key:
        runtime.set_rag_api_key(rag_api_key)
    if rag_base_url:
        runtime.set_rag_base_url(rag_base_url)
    if project_root:
        runtime.set_project_root(project_root)

    # 项目约定 + 代码库索引：各自作为独立的 system 消息，保持主提示词稳定（对缓存友好）
    _root = project_root or runtime.get_project_root()
    project_ctx_msg = project_context.build_system_message(_root)
    repo_ctx_msg, repo_ctx = repo_context.build_system_message(_root, focus=user_task)

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT + memory_context + plan_context}
    ]

    run_index = None
    try:
        run_index = runtime.get_run_index()
    except Exception:                                        # noqa: BLE001
        pass
    if run_index:
        yield {"type": "run_started", "run_index": run_index}

    if project_ctx_msg:
        messages.append(project_ctx_msg)
        yield {
            "type": "project_context",
            "sources": project_context.load(_root)["sources"],
            "chars": len(project_ctx_msg["content"]),
            "truncated": False,
        }
    if repo_ctx_msg:
        messages.append(repo_ctx_msg)
        yield {
            "type": "repo_map",
            "files": repo_ctx["files"],
            "indexed": repo_ctx["indexed"],
            "symbols": repo_ctx["symbols"],
            "chars": len(repo_ctx["text"]),
            "languages": repo_ctx["languages"],
            "truncated": repo_ctx["truncated"],
        }

    # ===== 续跑：从 MySQL 里的 pending 恢复 =====
    pending_tool = None
    resumed = False
    if _resume:
        saved = runtime.get_pending_state()
        if saved and saved.get("messages"):
            messages = copy.deepcopy(saved["messages"])
            for m in messages:
                if m.get("role") == "assistant" and "reasoning_content" in m:
                    if not str(m.get("reasoning_content") or "").strip():
                        del m["reasoning_content"]
            _ensure_reasoning_consistency(messages)
            user_task = saved.get("task") or user_task
            if saved.get("run_index"):
                runtime.set_run_index(saved["run_index"])
            pending_tool = saved.get("pending_tool")
            runtime.clear_pending_state()
            resumed = True

    # ===== 历史消息（按 token 预算裁剪）=====
    if session_id and not resumed:
        MAX_HISTORY_TOKENS, CHAR_PER_TOKEN = 3000, 3
        all_history = db.get_messages(session_id)[:-1]
        history, total_chars = [], 0
        max_chars = MAX_HISTORY_TOKENS * CHAR_PER_TOKEN
        for m in reversed(all_history):
            content = m["content"]
            if m["role"] == "assistant":
                try:
                    obj = json.loads(content)
                    content = obj.get("answer", content)
                    names = []
                    for t in obj.get("tools") or []:
                        if t.get("type") == "tool_call" and t.get("name") not in names:
                            names.append(t.get("name"))
                    if names:
                        content += f"\n（上一轮调用了工具：{', '.join(names)}）"
                except Exception:                            # noqa: BLE001
                    pass
            elif m["role"] == "user":
                try:
                    obj = json.loads(content)
                    if isinstance(obj, dict) and "text" in obj:
                        content = obj["text"]
                except Exception:                            # noqa: BLE001
                    pass
            if len(content) > 1000:
                content = content[:1000] + "...（已截断）"
            if total_chars + len(content) > max_chars:
                break
            history.insert(0, {"role": m["role"], "content": content})
            total_chars += len(content)
        messages.extend(history)

    # ===== 当前任务 =====
    if not resumed:
        task_text = user_task + _format_attachments(attachments or [])
        if images:
            content: list = [{"type": "text", "text": task_text}]
            content += [
                {"type": "image_url", "image_url": {"url": img, "detail": "original"}}
                for img in images
            ]
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "user", "content": task_text})

    # ===== 回调：断点 / 确认 / 权限 =====
    def _persist(msgs: list[dict], step: int) -> None:
        runtime.clear_pending_state()
        state = {"messages": copy.deepcopy(msgs), "step": step, "task": user_task}
        if run_index:
            state["run_index"] = run_index
        runtime.set_pending_state(state)

    def _save_pending(msgs: list[dict], step: int, tool: dict, _unused: str) -> str:
        token = secrets.token_urlsafe(32)
        runtime.clear_pending_state()
        state = {
            "messages": copy.deepcopy(msgs),
            "step": step,
            "task": user_task,
            "pending_tool": tool,
            "confirm_token": token,
        }
        if run_index:
            state["run_index"] = run_index
        runtime.set_pending_state(state)
        return token

    # ===== 构建图并执行 =====
    try:
        model = _build_model(provider_id, user_api_key, user_base_url, user_model)
    except Exception as e:                                   # noqa: BLE001
        yield {"type": "error", "content": f"❌ 模型初始化失败：{e}"}
        yield {"type": "done"}
        return

    import toolkit

    specs = toolkit.selected_specs(task=user_task, full=planner_plan is not None)
    tools_list = toolkit.build_tools(specs)

    ctx = GraphContext(
        model=model,
        tools=tools_list,
        tool_map={t.name: t for t in tools_list},
        session_id=session_id,
        run_index=run_index,
        auto_confirm=auto_confirm,
        max_steps=max_steps,
        persist=_persist,
        save_pending=_save_pending,
        gate=_permission_gate,
        confirm_event=_confirm_event,
        budget_tokens=CONTEXT_BUDGET_TOKENS,
        keep_recent=CONTEXT_KEEP_RECENT_TOOL_RESULTS,
        execute=execute_tool,
    )

    state = {
        "messages": messages,
        "step": 0,
        "usage": {},
        "pending": pending_tool,
        "confirm_token": None,
        "finished": False,
    }

    usage: dict = {}
    final_state: dict = {}
    try:
        for kind, payload in run_graph(ctx, state, max_steps):
            if kind == "event":
                yield payload
            else:
                final_state = payload
                usage = payload.get("usage") or usage
    except RunCancelled as exc:
        # 取消可能发生在工具执行中途：此时 assistant 消息里可能有「还没执行」的
        # tool_calls，直接存会变成悬空调用（续跑必然 400）。图里已裁剪好带出来。
        pending_msgs = getattr(exc, "messages", None) or final_state.get("messages") or messages
        _persist(pending_msgs, int(getattr(exc, "step", 0) or 0))
        yield {"type": "cancelled", "content": "任务已停止（可从断点继续）"}
        yield {"type": "done"}
        return
    except Exception as e:                                   # noqa: BLE001
        err = str(e)
        lower = err.lower()
        if "401" in err or "invalid_api_key" in lower or "incorrect api key" in lower:
            friendly = "❌ API Key 无效或已过期，请检查设置"
        elif "404" in err or "model not found" in lower:
            friendly = "❌ 模型名不存在，请检查设置"
        elif "429" in err or "rate limit" in lower or "quota" in lower:
            friendly = "❌ 额度不足或请求过频，请稍后重试"
        elif "connection" in lower or "timeout" in lower or "timed out" in lower:
            friendly = "❌ 网络连接失败，请检查网络"
        elif "recursion" in lower:
            friendly = f"⚠️ 已达到最大步数 {max_steps}，任务可能未完成。"
        elif "400" in err:
            if "context" in lower or "maximum context" in lower or "too long" in lower:
                friendly = (
                    "❌ 上下文超出模型窗口。请开一个新会话，"
                    "或把超长内容存成文件后用 read_file 分段查看。"
                )
            else:
                friendly = f"❌ 请求参数错误：{err[:150]}"
        else:
            friendly = f"❌ 调用失败：{err[:200]}"
        try:
            _persist(final_state.get("messages", messages), int(final_state.get("step") or 0))
        except Exception:                                    # noqa: BLE001
            pass
        yield {"type": "error", "content": friendly}
        yield {"type": "done"}
        return

    # 挂起等确认：确认事件已在图里发出，这里直接收尾
    if final_state.get("pending"):
        return

    # 熔断终止：保留断点（用户/开发者可以据此排查或续跑），不清理
    if final_state.get("stop"):
        return

    total = usage or {}
    yield {
        "type": "token_usage",
        "prompt_tokens": total.get("prompt_tokens", 0),
        "completion_tokens": total.get("completion_tokens", 0),
        "total_tokens": total.get("total_tokens", 0),
        "cache_hit_tokens": total.get("cache_hit_tokens", 0),
        "cache_miss_tokens": total.get("cache_miss_tokens", 0),
        "cache_hit_rate": cache_hit_rate(
            total.get("cache_hit_tokens", 0), total.get("prompt_tokens", 0)
        ),
        "steps": final_state.get("step", 0),
    }
    runtime.clear_pending_state()
    yield {"type": "done"}
