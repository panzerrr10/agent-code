#!/usr/bin/env python3
"""agent-code CLI —— 命令行接口。

用法：
    agent-code "任务描述"                    # 单次任务（当前目录）
    agent-code -p /path/to/project "任务"    # 指定项目
    agent-code -i                             # 交互模式（REPL）
    agent-code --help                         # 帮助
"""

import argparse
import os
import sys

from dotenv import load_dotenv

load_dotenv()


def load_config():
    """从环境变量 / .env / config_providers 加载配置。

    兜底值来自 config_providers 的默认提供商（用户在界面里配置的那家），
    config.py 里的 BASE_URL / MODEL 常量已删除。
    命令行 --api-key / --base-url / --model 仍可覆盖（开发者便利）。
    """
    from config_providers import get_default_provider_id, get_provider

    cfg = get_provider(get_default_provider_id())
    return {
        "api_key": os.getenv("API_KEY") or cfg["api_key"],
        "base_url": os.getenv("BASE_URL") or cfg["base_url"],
        "model": os.getenv("MODEL") or cfg["default_model"],
        "rag_api_key": os.getenv("RAG_API_KEY") or "",
        "rag_base_url": os.getenv("RAG_BASE_URL") or "",
    }


def print_event(event, verbose=True):
    """打印流式事件。"""
    t = event.get("type")
    if t == "text":
        print(event.get("content", ""), end="", flush=True)
    elif t == "tool_call" and verbose:
        print(f"\n\033[36m[工具]\033[0m {event.get('name')}({event.get('args')})")
    elif t == "tool_result" and verbose:
        content = event.get("content", "")
        if len(content) > 300:
            content = content[:300] + "...（截断）"
        print(f"\033[90m[结果]\033[0m {content}")
    elif t == "plan_update" and verbose:
        plan = event.get("plan", [])
        if plan:
            print("\n\033[33m[计划]\033[0m")
            for i, item in enumerate(plan, 1):
                status = item.get("status", "pending")
                icon = {"done": "✓", "failed": "✗", "doing": "⏳", "pending": " "}.get(status, " ")
                print(f"  {i}. [{icon}] {item.get('step')}")
    elif t == "token_usage" and verbose:
        total = event.get("total_tokens", 0)
        print(f"\n\033[90m[Token] {total} tokens\033[0m")
    elif t == "error":
        print(f"\n\033[31m[错误]\033[0m {event.get('content')}")
    elif t == "done":
        print()


def run_task(task: str, project: str, config: dict, verbose: bool = True, confirmed: bool = False, _resume: bool = False, session_id: int = None):
    """执行一次任务。

    confirmed=True 表示「本次运行内所有危险操作都不再询问」（对应 --yes，用于自动化）。
    正常的交互确认不走这个开关：用户敲 y 之后会用 _resume=True 续跑，
    挂起的那个工具由续跑分支执行，后续新出现的危险操作仍会再次询问。
    """
    import runtime
    from agent import run_agent_stream

    # 先绑定会话，再写项目根目录：粘性状态按会话分桶，顺序反了会落到默认桶
    runtime.set_session_id(session_id)
    runtime.set_project_root(os.path.abspath(project))
    if config["api_key"]:
        runtime.set_api_key(config["api_key"])
    if config["base_url"]:
        runtime.set_base_url(config["base_url"])
    if config["rag_api_key"]:
        runtime.set_rag_api_key(config["rag_api_key"])
    if config["rag_base_url"]:
        runtime.set_rag_base_url(config["rag_base_url"])

    if not confirmed:
        print(f"\n\033[32m[agent-code]\033[0m 项目: {project}")
        print(f"\033[32m[agent-code]\033[0m 会话: #{session_id or '临时'}")
        print(f"\033[32m[agent-code]\033[0m 任务: {task}")
        print("\033[90m" + "─" * 60 + "\033[0m")

    confirm_event = None  # 记录遇到的 confirm 事件

    import json

    import db

    # 保存用户消息（_resume 时不存，因为上一轮已存过）
    if session_id and not _resume:
        db.save_message(session_id, "user", task)

    # 自动命名：如果会话还是"CLI 会话"，用任务前 20 字命名
    if session_id and not _resume:
        sessions = db.get_all_sessions()
        for s in sessions:
            if s["id"] == session_id and s["title"] == "CLI 会话":
                new_title = task[:20] + ("..." if len(task) > 20 else "")
                db.update_session_title(session_id, new_title)
                break

    try:
        import runtime as _rt_prefix
        full_answer = _rt_prefix.get_pending_text() or ""
        _rt_prefix.clear_pending_text()
        tool_events = []
        token_usage = None

        for event in run_agent_stream(
            task,
            session_id=session_id,
            auto_confirm=confirmed,
            _resume=_resume,
            user_api_key=config["api_key"] or None,
            user_base_url=config["base_url"] or None,
            user_model=config["model"] or None,
            rag_api_key=config["rag_api_key"] or None,
            rag_base_url=config["rag_base_url"] or None,
            project_root=os.path.abspath(project),
        ):
            # 拦截 confirm_* 事件
            if event.get("type") in ("confirm_write", "confirm_command", "confirm_delete"):
                confirm_event = event
                # 保存 confirm 前的文本，确认后拼接
                import runtime as _rt_text
                _rt_text.set_pending_text(full_answer)
                break  # 停止本次循环

            # 收集事件（跟 app.py 一致）
            if event.get("type") == "text":
                full_answer += event.get("content", "")
            elif event.get("type") == "tool_call":
                tool_events.append({"type": "tool_call", "name": event.get("name"), "args": event.get("args")})
            elif event.get("type") == "tool_result":
                tool_events.append({"type": "tool_result", "content": event.get("content")})
            elif event.get("type") == "token_usage":
                token_usage = {
                    "prompt_tokens": event.get("prompt_tokens"),
                    "completion_tokens": event.get("completion_tokens"),
                    "total_tokens": event.get("total_tokens"),
                    "steps": event.get("steps"),
                }

            print_event(event, verbose=verbose)
    except KeyboardInterrupt:
        print("\n\033[33m[已中断]\033[0m")
        return
    except Exception as e:
        print(f"\n\033[31m[异常]\033[0m {e}")
        return

    # 保存 assistant 回答（即使无文本也存，避免历史缺条）
    if session_id and not confirm_event:  # confirm 中断时不存，避免重复
        saved_answer = full_answer if full_answer else "（已执行工具，无文本输出）"
        saved = json.dumps({
            "answer": saved_answer,
            "tools": tool_events,
            "tokens": token_usage,
        }, ensure_ascii=False)
        db.save_message(session_id, "assistant", saved)

    # 如果有 confirm 事件，交互式确认
    if confirm_event:
        t = confirm_event["type"]
        if t == "confirm_write":
            print(f"\n\033[33m⚠️ 需要确认写入：{confirm_event.get('path')}\033[0m")
            old = confirm_event.get("old_content", "")[:200]
            new = confirm_event.get("new_content", "")[:200]
            print(f"  \033[90m原内容（前200字）：{old}\033[0m")
            print(f"  \033[90m新内容（前200字）：{new}\033[0m")
        elif t == "confirm_command":
            print(f"\n\033[33m⚠️ 需要确认命令：{confirm_event.get('cmd')}\033[0m")
        elif t == "confirm_delete":
            print(f"\n\033[33m⚠️ 需要确认删除：{confirm_event.get('path')}\033[0m")

        try:
            answer = input("\033[36m确认？[y/N] \033[0m").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"

        if answer in ("y", "yes"):
            # 只确认「这一个」挂起操作：续跑时不再自动确认后续的危险操作
            print("\033[32m[已确认，继续]\033[0m")
            run_task(task, project, config, verbose=verbose, confirmed=False, _resume=True, session_id=session_id)
        else:
            print("\033[31m[已取消]\033[0m")


def interactive_mode(project: str, config: dict, verbose: bool, session_id: int = None, auto_confirm: bool = False):
    """交互模式（REPL）。"""
    import db

    # 用现有会话 或 新建
    if session_id:
        sessions = db.get_all_sessions()
        title = next((s["title"] for s in sessions if s["id"] == session_id), "CLI 会话")
    else:
        session_id = db.create_session("CLI 会话")
        title = "CLI 会话"

    print("\033[32magent-code 交互模式\033[0m")
    print(f"项目: {os.path.abspath(project)}")
    print(f"会话: #{session_id} ({title})")
    print("命令: /history 历史 /new 新会话 /status Git状态 /log 最近提交 /undo [N] 回滚N步 /exit 退出\n")

    while True:
        try:
            task = input("\033[36magent-code>\033[0m ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见")
            break

        if not task:
            continue

        if task in ("exit", "quit", "q", "/exit"):
            print("再见")
            break

        if task == "/history":
            msgs = db.get_messages(session_id)
            print(f"\n\033[33m[历史] 共 {len(msgs)} 条\033[0m")
            for m in msgs[-10:]:
                content = m["content"][:100]
                print(f"  [{m['role']}] {content}")
            print()
            continue

        if task == "/new":
            session_id = db.create_session("CLI 会话")
            print(f"\033[32m[新会话] #{session_id}\033[0m\n")
            continue

        if task == "/status":
            from tools import git_status
            print("\n\033[33m[Git Status]\033[0m")
            print(git_status())
            print()
            continue

        if task == "/log":
            from tools import git_log
            print("\n\033[33m[Git Log]\033[0m")
            print(git_log(5))
            print()
            continue

        if task.startswith("/undo"):
            from tools import git_rollback, git_status
            # 解析参数：/undo 或 /undo N
            parts = task.split()
            n = 1
            if len(parts) > 1:
                try:
                    n = int(parts[1])
                except ValueError:
                    print(f"\033[31m❌ 参数错误：{parts[1]}，应为整数\033[0m\n")
                    continue

            # 显示当前状态
            print("\n\033[33m[当前 Git 状态]\033[0m")
            print(git_status())

            # 二次确认
            print(f"\n\033[31m⚠️ 即将回滚 {n} 个 commit（git reset --hard HEAD~{n}）\033[0m")
            print("\033[31m⚠️ 当前所有未提交改动会丢失！\033[0m")
            try:
                answer = input("\033[36m确认回滚？[y/N] \033[0m").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = "n"

            if answer in ("y", "yes"):
                result = git_rollback(f"HEAD~{n}")
                print("\n\033[32m[已回滚]\033[0m")
                print(result)
            else:
                print("\033[90m[已取消]\033[0m")
            print()
            continue

        run_task(
            task,
            project,
            config,
            verbose=verbose,
            confirmed=auto_confirm,
            session_id=session_id,
        )


def main():
    parser = argparse.ArgumentParser(
        prog="agent-code",
        description="代码开发 Agent（CLI 版）",
    )
    parser.add_argument("task", nargs="?", help="任务描述")
    parser.add_argument("-p", "--project", default=".", help="项目路径（默认当前目录）")
    parser.add_argument("-i", "--interactive", action="store_true", help="交互模式")
    parser.add_argument("-s", "--session", type=int, help="指定会话 ID（默认新建）")
    parser.add_argument("-q", "--quiet", action="store_true", help="安静模式（只输出最终答案）")
    parser.add_argument("--api-key", help="API Key（覆盖 .env）")
    parser.add_argument("--base-url", help="Base URL（覆盖 .env）")
    parser.add_argument("--model", help="模型名（覆盖 .env）")
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="自动化模式：本次运行内所有危险操作都不再询问（默认逐个确认）",
    )

    args = parser.parse_args()

    # 加载配置
    config = load_config()

    import db
    db.init_db()

    if args.api_key:
        config["api_key"] = args.api_key
    if args.base_url:
        config["base_url"] = args.base_url
    if args.model:
        config["model"] = args.model

    if not config["api_key"]:
        print("\033[31m❌ 未配置 API Key。请设置环境变量 API_KEY 或在 .env 里配置。\033[0m")
        sys.exit(1)

    verbose = not args.quiet

    if args.interactive:
        interactive_mode(args.project, config, verbose, session_id=args.session, auto_confirm=args.yes)
    else:
        if not args.task:
            parser.print_help()
            sys.exit(0)
        # 单次任务：默认新建会话
        import db
        sid = args.session if args.session else db.create_session("CLI 会话")
        run_task(
            args.task,
            args.project,
            config,
            verbose=verbose,
            confirmed=args.yes,
            session_id=sid,
        )


if __name__ == "__main__":
    main()
