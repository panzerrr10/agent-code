"""评估装置：把任务跑起来并收集指标。

## 设计要点

- **隔离**：评估用独立的 SQLite（``AGENT_DB_PATH``）和一份空的 MCP 配置，
  绝不碰真实的会话历史，也不受本机装的插件影响（否则结果不可复现）。
  ⚠️ ``paths.DB_PATH`` 是**导入时**读的，所以 ``isolate_env()`` 必须在
  import 任何 src 模块之前调用 —— ``eval/run.py`` 就是这么做的。
- **工作目录必须在 HOME 下**：路径白名单只放行用户主目录，放 /tmp 会被直接拒。
- **前置必须失败**：跑 agent 之前先验证一次「任务当前是失败的」。
  不验的话，通过率会虚高，而且验收脚本写坏了也发现不了。
- **自动确认**：评估里没人点确认，``auto_confirm=True`` 等价于本次运行强制等级 3。
  这是评估专用的放权，正常使用路径不受影响。
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

logger = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EVAL_TMP = None


def isolate_env() -> str:
    """把评估隔到临时目录。**必须在 import src 之前调用。**"""
    global _EVAL_TMP
    if _EVAL_TMP is not None:
        return _EVAL_TMP
    home = os.path.expanduser("~")
    d = tempfile.mkdtemp(prefix="agentcode-eval-", dir=home)
    os.environ["AGENT_DB_PATH"] = os.path.join(d, "eval.db")
    # 空 MCP 配置：评估要可复现，不能被本机装的插件影响
    mcp = os.path.join(d, "mcp.json")
    with open(mcp, "w", encoding="utf-8") as f:
        json.dump({"servers": {}, "inject_enabled": False}, f)
    os.environ["AGENT_MCP_PATH"] = mcp
    # 回收站同样要隔离：评估里每条任务都会改文件、每次改动都留备份，
    # 不隔离的话跑一轮就往用户真实的 data/.trash 里塞一批垃圾。
    os.environ["AGENT_TRASH_DIR"] = os.path.join(d, "trash")
    src = os.path.join(ROOT, "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    _EVAL_TMP = d

    # **保证任何退出路径都会清掉它。**
    #
    # 原来只靠 ``eval/run.py`` 末尾手动调一次 ``cleanup_env()``，而且那行
    # **不在 finally 里** —— 一按 Ctrl-C、一抛异常就直接泄漏。
    # 实测后果：用户 home 下攒了 71 个 ``agentcode-eval-*``，
    # 每个只剩一个 8KB 的 mcp.json（占了 568K 的目录项，也脏了 home）。
    # atexit 覆盖正常退出、异常退出与 KeyboardInterrupt（它们都会走到解释器关闭）。
    # 覆盖不到的是 SIGKILL —— 那种情况无解。
    atexit.register(cleanup_env)
    return d


def cleanup_env():
    if _EVAL_TMP and os.path.isdir(_EVAL_TMP):
        shutil.rmtree(_EVAL_TMP, ignore_errors=True)


# ---------------------------------------------------------------- 仓库


def _run(cmd: str, cwd: str, timeout: int = 120) -> tuple[int, str]:
    try:
        p = subprocess.run(
            cmd, cwd=cwd, shell=True, capture_output=True, text=True, timeout=timeout,
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"（命令超时 {timeout}s）"
    except Exception as e:                                   # noqa: BLE001
        return 125, f"（命令执行失败：{e}）"


def prepare_repo(task, base_dir: str) -> str:
    """建一个临时仓库并提交初始状态。

    ``git init`` 是必要的：agent 的 git_* 工具需要有仓库，
    而且**任务改动全都能用 git diff 看出来**，便于事后复盘。
    """
    workdir = tempfile.mkdtemp(prefix=f"task-{task.id}-", dir=base_dir)
    for rel, content in task.files.items():
        path = os.path.join(workdir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    _run("git init -q", workdir)
    _run("git config user.email eval@local", workdir)
    _run("git config user.name eval", workdir)
    _run("git add -A && git commit -q -m 'initial state'", workdir)
    return workdir


def check_task(task, workdir: str) -> tuple[bool, str]:
    """跑验收命令。可选地再跑一条额外检查。"""
    rc, out = _run(task.check_cmd, workdir, timeout=120)
    if rc != 0:
        return False, f"验收失败（退出码 {rc}）：{out.strip()[-400:]}"
    if task.extra_check:
        rc2, out2 = _run(task.extra_check, workdir, timeout=60)
        if rc2 != 0:
            return False, f"额外检查失败（退出码 {rc2}）：{out2.strip()[-300:]}"
    return True, "通过"


# ---------------------------------------------------------------- 跑一个任务


def run_task(task, *, provider_id=None, model=None, api_key=None, base_url=None,
             keep: bool = False, verbose: bool = False, budget: int = None) -> dict:
    """跑一个任务并返回结果 dict。

    ``budget``：覆盖上下文预算。**把预算调小是唯一能可靠验证压缩路径的办法** ——
    正常任务里 agent 会用 search_files / multi_edit 抄近路，上下文根本涨不起来
    （实测 17 条任务、从易到难，compactions 全是 0）。
    """
    isolate_env()
    import agent  # noqa: PLC0415
    import db  # noqa: PLC0415
    import runtime  # noqa: PLC0415

    if budget:
        agent.CONTEXT_BUDGET_TOKENS = int(budget)

    db.init_db()
    base = tempfile.mkdtemp(prefix="evalrun-", dir=os.path.expanduser("~"))
    workdir = prepare_repo(task, base)

    result = {
        "task": task.id,
        "title": task.title,
        "tags": list(task.tags or []),
        "passed": False,
        "reason": "",
        "steps": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "duration_s": 0.0,
        "tool_calls": 0,
        "compactions": 0,
        "error": "",
        "workdir": workdir if keep else "",
    }

    try:
        # ---- 前置：任务当前必须是失败的 ----
        pre_ok, pre_msg = check_task(task, workdir)
        if pre_ok:
            result["reason"] = "前置检查未失败 —— 任务本来就是通过的，验收脚本可能写错了"
            return result

        # ---- 建独立的工作区 + 会话 ----
        ws = db.create_workspace(f"eval-{task.id}", workdir)
        wid = ws if isinstance(ws, int) else ws["id"]
        sess = db.create_session(f"eval-{task.id}", workspace_id=wid)
        sid = sess if isinstance(sess, int) else sess["id"]
        runtime.set_session_id(sid)

        # ---- 跑 agent（带超时保护）----
        events = []
        err_box = {}

        def _consume():
            try:
                for ev in agent.run_agent_stream(
                    task.prompt,
                    session_id=sid,
                    project_root=workdir,
                    auto_confirm=True,        # 评估里没人点确认
                    provider_id=provider_id,
                    user_model=model,
                    user_api_key=api_key,
                    user_base_url=base_url,
                ):
                    events.append(ev)
                    if verbose:
                        _print_event(ev)
            except Exception as e:                            # noqa: BLE001
                err_box["error"] = f"{type(e).__name__}: {e}"

        t0 = time.time()
        th = threading.Thread(target=_consume, daemon=True)
        th.start()
        th.join(timeout=task.timeout)
        if th.is_alive():
            runtime.request_cancel(sid)
            th.join(timeout=30)
            result["error"] = f"任务超时（{task.timeout}s），已请求取消"
        result["duration_s"] = round(time.time() - t0, 1)
        if err_box.get("error"):
            result["error"] = err_box["error"]

        # ---- 收集指标 ----
        for ev in events:
            t = ev.get("type")
            # 只数 tool_call：tool_result 是同一次调用的另一半，一起数会翻倍
            if t == "tool_call":
                result["tool_calls"] += 1
            elif t == "context_stats":
                result["compactions"] += 1
            elif t == "token_usage":
                # 注意事件名是 token_usage 而不是 tokens，且 done 事件**不带指标**
                result["steps"] = ev.get("steps") or result["steps"]
                result["prompt_tokens"] = ev.get("prompt_tokens") or result["prompt_tokens"]
                result["completion_tokens"] = (
                    ev.get("completion_tokens") or result["completion_tokens"])
            elif t == "error":
                result["error"] = result["error"] or str(ev.get("content"))[:300]

        # ---- 验收 ----
        ok, msg = check_task(task, workdir)
        result["passed"] = ok
        result["reason"] = msg
        result["diff_stat"] = _diff_stat(workdir)
        return result

    finally:
        if not keep:
            shutil.rmtree(base, ignore_errors=True)
        else:
            logger.info("保留工作目录：%s", workdir)


def _diff_stat(workdir: str) -> str:
    rc, out = _run("git diff --stat", workdir)
    return out.strip().splitlines()[-1] if rc == 0 and out.strip() else ""


def _print_event(ev):
    t = ev.get("type")
    if t == "tool_call":
        print(f"      → {ev.get('name')} {str(ev.get('args'))[:80]}")
    elif t == "context_stats":
        print(f"      📉 {ev.get('before_tokens')} → {ev.get('after_tokens')} tokens")
    elif t == "text":
        print(f"      💬 {str(ev.get('content'))[:100]}")


# ---------------------------------------------------------------- 批量


def run_all(tasks, *, out_path=None, on_result=None, **kwargs) -> dict:
    """依次跑若干任务，返回汇总结果。"""
    isolate_env()
    results = []
    for i, task in enumerate(tasks, 1):
        print(f"\n[{i}/{len(tasks)}] {task.id} —— {task.title}")
        r = run_task(task, **kwargs)
        results.append(r)
        mark = "✅" if r["passed"] else "❌"
        print(f"   {mark} {r['reason'][:110]}")
        print(f"      步数={r['steps']} 工具调用={r['tool_calls']} "
              f"tokens={r['prompt_tokens']}+{r['completion_tokens']} "
              f"耗时={r['duration_s']}s 压缩={r['compactions']}")
        if on_result:
            on_result(r)

    summary = summarize(results)
    payload = {"summary": summary, "results": results}
    if out_path:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\n结果已写入 {out_path}")
    return payload


def summarize(results: list) -> dict:
    n = len(results) or 1
    passed = sum(1 for r in results if r["passed"])
    return {
        "tasks": len(results),
        "passed": passed,
        "pass_rate": round(passed / n, 3),
        "total_steps": sum(r["steps"] for r in results),
        "avg_steps": round(sum(r["steps"] for r in results) / n, 1),
        "total_tokens": sum(r["prompt_tokens"] + r["completion_tokens"] for r in results),
        "total_tool_calls": sum(r["tool_calls"] for r in results),
        "total_duration_s": round(sum(r["duration_s"] for r in results), 1),
        "compactions": sum(r.get("compactions", 0) for r in results),
    }
