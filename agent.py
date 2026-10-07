import json
import os

from config import MAX_STEPS
from config_providers import (
    DEFAULT_PROVIDER,
    get_extra_body,
    get_provider,
    get_provider_id_by_base_url,
)
from tools import execute_tool, get_tools_schema
from utils import call_llm_with_retry, get_llm_client


def _get_default_client():
    """按默认 provider 取客户端（进程内复用，不再每次 new）。"""
    cfg = get_provider(DEFAULT_PROVIDER)
    if not cfg.get("api_key"):
        raise RuntimeError(
            f"默认 provider {DEFAULT_PROVIDER} 的 api_key 未配置，请检查 .env"
        )
    return get_llm_client(cfg["api_key"], cfg["base_url"])


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
9. **改代码一律用 edit_file**：read_file（≤200 行）或 read_file_range（>200 行，分段读）看内容，然后 edit_file 精确替换。只在创建新文件时用 create_file。**禁止用 run_command 改代码**（grep/sed/awk 等会触发用户确认弹窗，打断任务）。edit_file 的 old_text 必须精确匹配（含缩进）。
10. **工具失败时**：第一次失败 → 检查参数后重试 1 次；仍失败 → 换一种方法尝试；两次不同方法都失败 → 报告"无法完成"给用户，不要无限重试。
11. **测试驱动**：改完代码后必须用 `run_tests` 验证；如果测试失败，分析错误并修复，最多重试 3 次；3 次仍失败就报告给用户，附上错误信息。
12. **先说明再做（强制）**：在调用 write_file / delete_file / run_command 这些会弹确认的工具之前，**必须先输出一句人类可读的说明**，格式如："📝 我要 <动作>：<目标>，需要你确认。"。**不要沉默地直接调工具**。这条是硬性要求，不是建议。

## 命令执行规范（重要）
- 执行 run_command 时，**不要写 `cd /workspace`**，系统会自动切换工作目录
- **不要用 `&&`、`|`、`;`、`>` 连接多个命令**，这些会触发安全拦截
- 正确写法：直接写 `python -m unittest test_bmi.py -v`
- 错误写法：`cd /workspace && python -m unittest test_bmi.py -v`
- 如果命令需要输出重定向，用 run_python 执行 Python 代码代替

## 知识库规范
- **何时用**：用户提到"上传的文档" / "知识库" / "之前传的文件"，或问题可能与上传文档有关时，必须用 search_knowledge。不要用 search_files 或 run_command 去找用户上传的文档——它们不在工作区文件系统里，在向量库里。只有在用户明确说"工作区的文件" / "某个目录下的文件" 时，才用 search_files / list_dir。
- **怎么搜**：query 用核心关键词，如"潘宗 教育经历"。找不到就换关键词重试，最多试 2 次。
- **怎么用结果**：严格基于返回内容回答。返回"未找到"就告诉用户"知识库中没有这方面的信息"。绝不根据"印象"或"常识"补充，不确定就说"不确定"。

## 测试驱动开发（重要）
- 每次改代码后，**必须调 run_tests 验证**
- 如果测试通过 → 报告"✅ 测试通过"
- 如果测试失败 → 分析错误 → 用 edit_file 修复 → 再跑 run_tests
- 最多重试 3 次（分析→修→跑，算 1 轮）
- 3 轮仍失败 → 停止并报告用户："无法自动修复，错误信息如下：..."
- **不要**在测试失败时直接告诉用户"任务完成"

## 路径
- 工作区是 `/workspace/`（= 用户的 home 目录）。
- 用 `/workspace/xxx` 访问用户 home 下的文件，如 `/workspace/PycharmProjects/Agent/app.py`。
- 也可以用相对路径（如 `foo.py`），默认在 `/workspace/` 下。
- 敏感目录（`.ssh`、`.config`、`.cache` 等）禁止访问。

## 输出
- 执行代码后，在回答里展示用的代码，再给结果。
- 完成任务后，用简洁的总结结束。
"""


def _ensure_reasoning_consistency(messages):
    """
    DeepSeek 推理模型：如果 messages 里【任何一条】assistant 消息带 reasoning_content，
    则【所有】assistant 消息都必须带该字段（值可以是空字符串，但字段必须存在）。
    否则续跑会 400：The `reasoning_content` in the thinking mode must be passed back to the API.
    本函数就地补全，保证 messages 内部一致（不改变消息结构）。
    """
    _has_any_rc = any(
        m.get("role") == "assistant" and "reasoning_content" in m
        for m in messages
    )
    if _has_any_rc:
        for m in messages:
            if m.get("role") == "assistant" and "reasoning_content" not in m:
                m["reasoning_content"] = ""


def run_agent_stream(
    user_task: str,
    session_id: int = None,
    provider_id: str = None,
    max_steps: int = MAX_STEPS,
    confirmed_delete: bool = False,
    planner_plan: list = None,
    user_api_key: str = None,
    user_base_url: str = None,
    user_model: str = None,
    project_root: str = None,
    rag_api_key: str = None,
    rag_base_url: str = None,
    images: list = None,
    _resume: bool = False,
):
    """
    Agent 主循环，流式 yield 事件（供 Flask SSE 用）。
    yield 的每个元素是 dict：
      {"type": "text", "content": "..."}       # 文本片段
      {"type": "tool_call", "name": "...", "args": {...}}  # 工具调用
      {"type": "tool_result", "content": "..."} # 工具结果
      {"type": "done"}                          # 结束
      {"type": "error", "content": "..."}       # 错误
    """
    import runtime as _rt_session

    # 绑定当前会话：项目根目录 / 计划 / pending 等粘性状态均按会话分桶，
    # 避免并发会话互相覆盖（多标签页同时跑任务时尤其重要）。
    _rt_session.set_session_id(session_id)
    # 注册本次运行的取消标志（全新 Event，上一次运行的残留取消状态不会误伤本次）
    _rt_session.register_run(session_id)

    import db

    memories = db.get_all_memories()
    memory_context = ""
    if memories:
        memory_lines = ["\n\n## 你的长期记忆（关于用户的偏好和重要信息）"]
        for m in memories:
            memory_lines.append(f"- {m['content']}")
        memory_context = "\n".join(memory_lines)
    # 如果有 Planner 计划，加进 System Prompt
    plan_context = ""
    if planner_plan:
        plan_lines = ["\n\n## 已由 Planner 生成的计划（请严格按此执行）"]
        for i, s in enumerate(planner_plan, 1):
            plan_lines.append(f"{i}. {s}")
        plan_lines.append(
            "\n**请按此计划执行，不要重新调用 create_plan**。每完成一步，调用 update_plan 更新状态。"
        )
        plan_context = "\n".join(plan_lines)
    # 把用户 API Key / Base URL 同步到 runtime（供 RAG / tools 使用）
    import runtime
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

    # 动态选择 client 和 model
    default_cfg = get_provider(DEFAULT_PROVIDER)
    if user_api_key:
        actual_client = get_llm_client(
            user_api_key, user_base_url or default_cfg["base_url"]
        )
        actual_model = user_model or default_cfg["default_model"]
    else:
        actual_client = _get_default_client()
        actual_model = user_model or default_cfg["default_model"]

    # 按 provider 条件化下发额外参数：DeepSeek 的 enable_search 属于厂商专属字段，
    # 原样发给 OpenAI / 百炼 / 智谱 会被判定为未知参数直接 400。
    effective_provider_id = provider_id or get_provider_id_by_base_url(
        user_base_url or default_cfg["base_url"]
    )
    _extra_body = get_extra_body(effective_provider_id)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT + memory_context + plan_context}
    ]

    # 【消费端】如果 _resume=True 且有 pending state，用保存的 messages 覆盖
    _resumed_from_pending = False
    _pending = None
    if _resume:
        import runtime as _rt_resume
        _pending = _rt_resume.get_pending_state()
        if _pending and _pending.get("messages"):
            # 【修复 Bug1】用 deepcopy 切断引用，避免后续修改污染 pending_state 里的原始列表
            import copy
            messages = copy.deepcopy(_pending["messages"])
            # 兼容旧 pending_state：
            # 如果 assistant 消息带 reasoning_content 但是空/不完整，
            # DeepSeek 会 400。干脆清掉所有空的 reasoning_content 字段，让 API 不校验。
            # 注意：这是在"续跑恢复旧数据"时才用，新数据会带完整 reasoning_content。
            for m in messages:
                if m.get("role") == "assistant" and "reasoning_content" in m:
                    if not m.get("reasoning_content", "").strip():
                        del m["reasoning_content"]
            # 续跑时用 pending 里保存的原始任务，保证工具选择 / 上下文与中断前一致
            user_task = _pending.get("task") or user_task
            # 补上“缺失的 tool 消息”（避免悬空 tool_call 报 400）
            pending_tool = _pending.get("pending_tool")
            if pending_tool:
                tool_result = execute_tool(pending_tool["name"], pending_tool["args"])
                messages.append({
                    "role": "tool",
                    "tool_call_id": pending_tool["id"],
                    "content": tool_result,
                })
                # 让前端“执行详情”显示本次确认工具的结果
                yield {"type": "tool_result", "content": tool_result}
            # DeepSeek 推理模型：恢复的旧 pending_state 可能只有部分 assistant 带
            # reasoning_content，统一补全，保证续跑不 400（不改变消息结构）
            _ensure_reasoning_consistency(messages)
            _resumed_from_pending = True
            _rt_resume.clear_pending_state()
            print(f"\033[35m[RESUME]\033[0m 从 pending state 恢复，messages 长度={len(messages)}，补 tool 结果")

    # 【修复】续跑时不再从 db 读"上一个任务"的历史，避免 LLM 过度联想、
    # messages 累积、token 暴涨。pending_state 里的 messages 已含当前任务完整上下文。
    if session_id and not _resumed_from_pending:
        import db

        # 按 token 数动态裁剪历史（替代固定 6 条）
        # 粗估：1 token ≈ 3 字符（中文），1 token ≈ 4 字符（英文），取保守值 3
        MAX_HISTORY_TOKENS = 3000
        CHAR_PER_TOKEN = 3

        all_history = db.get_messages(session_id)[:-1]  # 排除最后一条（当前任务）
        history_msgs = []
        total_chars = 0
        max_chars = MAX_HISTORY_TOKENS * CHAR_PER_TOKEN
        for m in reversed(all_history):
            content = m["content"]
            if m["role"] == "assistant":
                try:
                    import json as _json

                    obj = _json.loads(content)
                    if "answer" in obj:
                        content = obj["answer"]
                    # 保留本轮工具调用摘要，避免跨轮丢失「上一轮做过什么」
                    _tool_names = []
                    for _t in obj.get("tools") or []:
                        if _t.get("type") == "tool_call" and _t.get("name"):
                            if _t["name"] not in _tool_names:
                                _tool_names.append(_t["name"])
                    if _tool_names:
                        content += f"\n（上一轮调用了工具：{', '.join(_tool_names)}）"
                except Exception:
                    pass
            elif m["role"] == "user":
                # 带图片的用户消息存的是 JSON（含 Base64），只取文本，避免污染上下文
                try:
                    import json as _json

                    obj = _json.loads(content)
                    if isinstance(obj, dict) and "text" in obj:
                        img_count = len(obj.get("images") or [])
                        content = obj.get("text", "")
                        if img_count:
                            content += f"（用户上传了 {img_count} 张图片）"
                except Exception:
                    pass
            # 单条截断（防止单条过长）
            if len(content) > 1000:
                content = content[:1000] + "...（已截断）"
            # 累计字符数，超限就停
            if total_chars + len(content) > max_chars:
                break
            history_msgs.insert(0, {"role": m["role"], "content": content})
            total_chars += len(content)

        messages.extend(history_msgs)
        print(f"[HISTORY] 裁剪后 messages 长度={len(messages)}, 历史条数={len(history_msgs)}")
    elif _resumed_from_pending:
        print(f"[HISTORY] 续跑模式：跳过 db 历史，messages 长度={len(messages)}")

    # 加当前任务（若从 pending 恢复，则跳过——task 已在 messages 里）
    if not _resumed_from_pending:
        if images:
            # 多模态：content 改成数组 [text 块 + image_url 块]
            # 注意：仅打印数量，绝不打印 Base64，防止爆日志
            print(f"[IMG] received {len(images)} images")
            content = [{"type": "text", "text": user_task}]
            for img in images:
                # img 是前端传来的 data URL（data:image/png;base64,...）
                content.append(
                    {"type": "image_url", "image_url": {"url": img, "detail": "original"}}
                )
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "user", "content": user_task})

    # 累计 tokens（只定义一次）
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_tokens = 0
    failure_tracker = {}

    _start_step = _pending.get("step", 0) if _resume and _pending else 0
    for step in range(_start_step, max_steps):
        # 取消检查点 1：进入新的一步之前
        if runtime.is_cancelled():
            yield {"type": "cancelled", "content": "任务已停止"}
            yield {"type": "done"}
            return
        try:
            # 根据任务动态选工具
            selected_tools = get_tools_schema(
                task=user_task,
                full=(planner_plan is not None),  # 有 Planner 计划 → 全发
            )
            # DeepSeek 推理模型：调用 LLM 前保证发给 API 的 messages 的
            # reasoning_content 字段内部一致，否则 400
            _ensure_reasoning_consistency(messages)
            _llm_kwargs = dict(
                model=actual_model,
                messages=messages,
                tools=selected_tools,
                tool_choice="auto",
                temperature=0,
                stream=True,  # ← 流式
                stream_options={"include_usage": True},  # ← 获取 token 用量
            )
            if _extra_body:
                _llm_kwargs["extra_body"] = _extra_body
            stream = call_llm_with_retry(actual_client, **_llm_kwargs)

            # 累积流式输出
            content_buffer = ""
            reasoning_buffer = ""   # ← 新增：DeepSeek 推理模型的思维链
            tool_calls_buffer = {}  # {index: {...}}
            usage = None

            for chunk in stream:
                # 取消检查点 2：流式输出过程中（长回答可能持续几十秒）
                if runtime.is_cancelled():
                    try:
                        stream.close()
                    except Exception:
                        pass
                    yield {"type": "cancelled", "content": "任务已停止"}
                    yield {"type": "done"}
                    return
                # 处理 usage（最后一个 chunk 带）
                if hasattr(chunk, "usage") and chunk.usage:
                    usage = chunk.usage

                if not chunk.choices:
                    continue

                delta = chunk.choices[0].delta
                if delta.content:
                    content_buffer += delta.content

                # 捕获 DeepSeek 推理模型的 reasoning_content
                if hasattr(delta, "reasoning_content") and delta.reasoning_content:
                    reasoning_buffer += delta.reasoning_content

                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_calls_buffer:
                            tool_calls_buffer[idx] = {
                                "id": "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        if tc.id:
                            tool_calls_buffer[idx]["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                tool_calls_buffer[idx]["function"][
                                    "name"
                                ] += tc.function.name
                            if tc.function.arguments:
                                tool_calls_buffer[idx]["function"][
                                    "arguments"
                                ] += tc.function.arguments

        except Exception as e:
            err_msg = str(e)
            err_lower = err_msg.lower()

            # 分类错误
            if (
                "401" in err_msg
                or "invalid_api_key" in err_lower
                or "incorrect api key" in err_lower
            ):
                friendly = "❌ API Key 无效或已过期，请检查设置"
            elif (
                "404" in err_msg
                or "model not found" in err_lower
                or "does not exist" in err_lower
            ):
                friendly = "❌ 模型名不存在，请检查设置"
            elif "429" in err_msg or "rate limit" in err_lower or "quota" in err_lower:
                friendly = "❌ 额度不足或请求过频，请稍后重试"
            elif (
                "connection" in err_lower
                or "timeout" in err_lower
                or "timed out" in err_lower
            ):
                friendly = "❌ 网络连接失败，请检查网络"
            elif "400" in err_msg:
                friendly = f"❌ 请求参数错误：{err_msg[:150]}"
            else:
                friendly = f"❌ 调用失败：{err_msg[:200]}"

            yield {"type": "error", "content": friendly}
            return
        # 累积 token
        if usage:
            total_prompt_tokens += usage.prompt_tokens or 0
            total_completion_tokens += usage.completion_tokens or 0
            total_tokens += usage.total_tokens or 0

        # 构造一个「模拟的 assistant_msg」（和你原来的数据结构一致）
        class _Msg:
            pass

        assistant_msg = _Msg()
        assistant_msg.content = content_buffer or None
        if tool_calls_buffer:
            _tc_list = []
            for idx in sorted(tool_calls_buffer.keys()):
                item = tool_calls_buffer[idx]
                _tc = _Msg()
                _tc.id = item["id"]
                _tc.type = "function"
                _tc.function = _Msg()
                _tc.function.name = item["function"]["name"]
                _tc.function.arguments = item["function"]["arguments"]
                _tc_list.append(_tc)
            assistant_msg.tool_calls = _tc_list
        else:
            assistant_msg.tool_calls = None

        # 情况 1：调工具
        if assistant_msg.tool_calls:
            _assistant_dict = {
                "role": "assistant",
                "content": assistant_msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in assistant_msg.tool_calls
                ],
            }
            # DeepSeek 推理模型：必须回传 reasoning_content，否则续跑 400
            if reasoning_buffer:
                _assistant_dict["reasoning_content"] = reasoning_buffer
            messages.append(_assistant_dict)
            # DeepSeek 推理模型：任何一条 assistant 带 reasoning_content，则所有 assistant 都必须带
            # （值可为空）。此处统一补全，保证随后所有 set_pending_state 保存的 messages 一致
            _ensure_reasoning_consistency(messages)

            # 【修复 400】保存 pending_state 前，重建 messages：只保留"已处理"的 tool_calls。
            # 一次返回 N 个 tool_calls，若第 K 个被拦截弹勾叉，则只处理了前 K 个；
            # 未处理的 tool_calls 没有对应 tool 响应，续跑时 LLM 会报 400：
            # "An assistant message with 'tool_calls' must be followed by tool messages..."
            # 注意：仅裁剪「当前这条」assistant 消息的未处理 tool_calls，
            # 之前步骤的 assistant 消息（及其 tool_calls）原样保留，避免误删导致旧 tool 消息悬空。
            # 先把当前 assistant 的 tool_call id 列表取出来，供下面的闭包按值捕获：
            # 闭包若直接引用 assistant_msg / step，会绑定到外层循环变量，
            # 一旦将来改成延迟调用就会取到错误的一轮数据。
            _all_tc_ids = [t.id for t in assistant_msg.tool_calls]

            def _save_pending_with_processed(
                _idx, _tc, _func_name, _func_args, _tc_ids=_all_tc_ids, _step=step
            ):
                import runtime as _rt_save

                _current_ids = set(_tc_ids)
                _processed_ids = set(_tc_ids[: _idx + 1])
                _modified_messages = []
                for _m in messages:
                    if _m.get("role") == "assistant" and _m.get("tool_calls"):
                        _filtered_tcs = [
                            _t
                            for _t in _m["tool_calls"]
                            # 之前步骤的 tool_calls（不属于当前 assistant）→ 保留
                            # 当前 assistant 的 tool_calls → 只保留已处理的
                            if _t["id"] not in _current_ids
                            or _t["id"] in _processed_ids
                        ]
                        if _filtered_tcs:
                            _new_m = dict(_m)
                            _new_m["tool_calls"] = _filtered_tcs
                            _modified_messages.append(_new_m)
                    else:
                        _modified_messages.append(_m)
                _rt_save.clear_pending_state()  # 【修复 Bug1】先清空旧的，避免多轮累积
                _rt_save.set_pending_state(
                    {
                        "messages": _modified_messages,
                        "step": _step,
                        "task": user_task,
                        "pending_tool": {
                            "id": _tc.id,
                            "name": _func_name,
                            "args": _func_args,
                        },
                    }
                )

            for idx, tc in enumerate(assistant_msg.tool_calls):
                # 取消检查点 3：执行任何工具之前（避免「已点停止却还在写文件」）
                if runtime.is_cancelled():
                    yield {"type": "cancelled", "content": "任务已停止"}
                    yield {"type": "done"}
                    return
                func_name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except Exception:
                    args = {}

                yield {"type": "tool_call", "name": func_name, "args": args}

                # 拦截 delete_file
                if func_name == "delete_file":
                    if confirmed_delete:
                        result = execute_tool(func_name, args)
                        yield {"type": "tool_result", "content": result}
                        # ✅ 失败跟踪
                        if isinstance(result, str) and result.startswith("❌"):
                            failure_tracker[func_name] = (
                                failure_tracker.get(func_name, 0) + 1
                            )
                            fail_count = failure_tracker[func_name]

                            if fail_count >= 3:
                                # 连续失败 3 次，强制停止
                                yield {
                                    "type": "text",
                                    "content": f"\n\n⚠️ 工具 `{func_name}` 已连续失败 3 次，停止执行。请检查任务描述或手动处理。",
                                }
                                yield {"type": "done"}
                                return
                            elif fail_count >= 2:
                                # 连续失败 2 次，注入提示
                                messages.append(
                                    {
                                        "role": "user",
                                        "content": f"⚠️ 系统提示：`{func_name}` 已连续失败 {fail_count} 次。请分析失败原因，换个方式重试，或告诉用户无法完成。",
                                    }
                                )
                        else:
                            # 成功，清零
                            failure_tracker[func_name] = 0

                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": result,
                            }
                        )
                        continue
                    else:
                        _save_pending_with_processed(idx, tc, func_name, args)
                        yield {
                            "type": "confirm_delete",
                            "path": args.get("path", ""),
                        }
                        return

                # 拦截 write_file
                if func_name == "write_file":
                    if confirmed_delete:
                        result = execute_tool(func_name, args)
                        yield {"type": "tool_result", "content": result}
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": result,
                            }
                        )
                        continue
                    else:
                        # 发确认请求，附带新旧内容对比
                        path = args.get("path", "")
                        new_content = args.get("content", "")

                        # 读取原内容
                        try:
                            old_content = ""
                            if os.path.exists(path):
                                with open(
                                    path, encoding="utf-8", errors="ignore"
                                ) as f:
                                    old_content = f.read()
                        except Exception:
                            old_content = "（无法读取原内容）"

                        _save_pending_with_processed(idx, tc, func_name, args)
                        yield {
                            "type": "confirm_write",
                            "path": path,
                            "old_content": old_content,
                            "new_content": new_content,
                        }
                        return
                        # 拦截 run_command
                if func_name == "run_command":
                    cmd = args.get("cmd", "")

                    # 检查是否安全命令
                    from tools import is_safe_command

                    if is_safe_command(cmd) or confirmed_delete:
                        # 安全命令 或 用户已确认 → 直接执行
                        result = execute_tool(func_name, args)
                        yield {"type": "tool_result", "content": result}
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": result,
                            }
                        )
                        continue
                    else:
                        # 不安全 → 弹确认
                        _save_pending_with_processed(idx, tc, func_name, args)
                        yield {
                            "type": "confirm_command",
                            "cmd": cmd,
                        }
                        return

                # 拦截 run_python
                if func_name == "run_python":
                    if confirmed_delete:
                        result = execute_tool(func_name, args)
                        yield {"type": "tool_result", "content": result}
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": result,
                            }
                        )
                        continue
                    else:
                        _save_pending_with_processed(idx, tc, func_name, args)
                        code_preview = (args.get("code") or "")[:300]
                        yield {
                            "type": "confirm_command",
                            "cmd": f"# run_python 执行代码：\n{code_preview}",
                        }
                        return

                # 拦截 git_commit
                if func_name == "git_commit":
                    if confirmed_delete:
                        result = execute_tool(func_name, args)
                        yield {"type": "tool_result", "content": result}
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": result,
                            }
                        )
                        continue
                    else:
                        _save_pending_with_processed(idx, tc, func_name, args)
                        yield {
                            "type": "confirm_command",
                            "cmd": f"git commit: {args.get('message', '')}",
                        }
                        return

                # 拦截 git_rollback
                if func_name == "git_rollback":
                    if confirmed_delete:
                        result = execute_tool(func_name, args)
                        yield {"type": "tool_result", "content": result}
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": result,
                            }
                        )
                        continue
                    else:
                        _save_pending_with_processed(idx, tc, func_name, args)
                        yield {
                            "type": "confirm_command",
                            "cmd": f"git rollback: {args}",
                        }
                        return

                # 其他工具，正常执行
                result = execute_tool(func_name, args)
                yield {"type": "tool_result", "content": result}
                if func_name in ("create_plan", "update_plan", "get_plan"):
                    from tools import get_plan_data

                    yield {
                        "type": "plan_update",
                        "plan": get_plan_data(),
                    }

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    }
                )
            continue

        # 情况 2：给出最终答案
        content = assistant_msg.content or ""
        yield {"type": "text", "content": content}

        # 发送 token 统计
        yield {
            "type": "token_usage",
            "prompt_tokens": total_prompt_tokens,
            "completion_tokens": total_completion_tokens,
            "total_tokens": total_tokens,
            "steps": step + 1,
        }
        import runtime as _rt_clear
        _rt_clear.clear_pending_state()
        yield {"type": "done"}
        return

    # 超出 max_steps
    yield {
        "type": "text",
        "content": f"\n\n⚠️ 已达到最大步数 {max_steps}，任务可能未完成。",
    }
    yield {
        "type": "token_usage",
        "prompt_tokens": total_prompt_tokens,
        "completion_tokens": total_completion_tokens,
        "total_tokens": total_tokens,
        "steps": max_steps,
    }
    yield {"type": "done"}

    import runtime as _rt_clear
    _rt_clear.clear_pending_state()
