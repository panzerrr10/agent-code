#!/usr/bin/env python3
"""评估集 CLI。

用法：

    # 跑全部任务（用 .env 里的默认 provider）
    python eval/run.py --all --out eval/results/baseline.json

    # 只跑某几个 / 某个标签
    python eval/run.py --tasks fix_failing_test,implement_function
    python eval/run.py --tags bugfix

    # 换模型对比
    python eval/run.py --all --model deepseek-reasoner --out eval/results/reasoner.json

    # 与基线对比（改完之后最常用）
    python eval/run.py --all --baseline eval/results/baseline.json

**为什么要有它**：单元测试能证明「代码没坏」，但证明不了「改完到底变好还是变差」。
有了这个，改提示词/工具/压缩阈值之后，第一件事就是跑一遍对比。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import harness  # noqa: E402
import tasks as tasks_mod  # noqa: E402

# ⚠️ 必须在 import 任何 src 模块之前隔离环境（paths.DB_PATH 是导入时读的）
harness.isolate_env()


def _resolve_provider(args):
    """决定用哪个 provider / 模型。默认和 Web 端一致（config_providers 的默认）。"""
    from config_providers import DEFAULT_PROVIDER, get_provider, get_provider_list

    pid = args.provider or DEFAULT_PROVIDER
    cfg = get_provider(pid)
    model = args.model or cfg.get("default_model")

    if not cfg.get("api_key"):
        print(f"⚠️  provider「{pid}」没有可用的 API Key。")
        print("    请在 .env 里配置，或在 Web 端「设置 → 模型与密钥」里填。")
        print("    可用的 provider：" + ", ".join(
            f"{p['id']}{'✓' if p['has_key'] else '✗'}" for p in get_provider_list()))
        sys.exit(2)

    print(f"provider={pid}  model={model}")
    return pid, model, cfg.get("api_key"), cfg.get("base_url")


def _compare(baseline: dict, current: dict):
    """与基线逐任务对比。"""
    b = {r["task"]: r for r in baseline.get("results", [])}
    c = {r["task"]: r for r in current.get("results", [])}
    bs, cs = baseline.get("summary", {}), current.get("summary", {})

    print("\n" + "=" * 66)
    print("与基线对比")
    print("=" * 66)
    print(f"{'任务':<26}{'基线':<8}{'本次':<8}变化")
    print("-" * 66)
    for tid in sorted(set(b) | set(c)):
        rb, rc = b.get(tid), c.get(tid)
        mb = "✅" if rb and rb["passed"] else ("❌" if rb else "—")
        mc = "✅" if rc and rc["passed"] else ("❌" if rc else "—")
        if rb and rc:
            if rb["passed"] != rc["passed"]:
                delta = "↑ 修复" if rc["passed"] else "↓ 退步"
            elif rc["steps"] != rb["steps"]:
                d = rc["steps"] - rb["steps"]
                delta = f"步数 {d:+d}"
            else:
                delta = "="
        else:
            delta = "（新增/缺失）"
        print(f"{tid:<26}{mb:<8}{mc:<8}{delta}")

    print("-" * 66)

    def line(label, key, fmt="{:.0f}", better_lower=True):
        vb, vc = bs.get(key), cs.get(key)
        if vb is None or vc is None:
            return
        try:
            d = vc - vb
        except TypeError:
            return
        arrow = ""
        if d != 0:
            good = (d < 0) if better_lower else (d > 0)
            arrow = "  ← 更好" if good else "  ← 变差"
        print(f"{label:<14}{fmt.format(vb):>10} → {fmt.format(vc):<10}{d:+.1f}{arrow}")

    line("通过率", "pass_rate", "{:.0%}")
    line("通过数", "passed")
    line("平均步数", "avg_steps", "{:.1f}")
    line("总 tokens", "total_tokens")
    line("工具调用", "total_tool_calls")
    line("总耗时(s)", "total_duration_s", "{:.0f}")
    line("压缩次数", "compactions")
    print("=" * 66)


def main():
    ap = argparse.ArgumentParser(description="agent-code 评估集")
    ap.add_argument("--all", action="store_true", help="跑全部任务")
    ap.add_argument("--tasks", help="逗号分隔的任务 id")
    ap.add_argument("--tags", help="逗号分隔的标签")
    ap.add_argument("--limit", type=int, help="最多跑几个")
    ap.add_argument("--provider", help="provider id（默认取配置里的默认）")
    ap.add_argument("--model", help="模型名（默认取 provider 的 default_model）")
    ap.add_argument("--out", help="结果写入的 JSON 路径")
    ap.add_argument("--baseline", help="基线 JSON，跑完自动对比")
    ap.add_argument("--keep", action="store_true", help="保留任务工作目录，便于复盘")
    ap.add_argument("--budget", type=int,
                    help="覆盖上下文预算（tokens）。调小可以强制触发压缩，"
                         "用来验证压缩路径不会把真实任务搞坏")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印每一步")
    ap.add_argument("--list", action="store_true", help="只列出任务")
    args = ap.parse_args()

    if args.list:
        print(f"{'id':<26}{'标签':<28}标题")
        print("-" * 80)
        for t in tasks_mod.ALL_TASKS:
            print(f"{t.id:<26}{','.join(t.tags):<28}{t.title}")
        return 0

    ids = args.tasks.split(",") if args.tasks else None
    tags = args.tags.split(",") if args.tags else None
    selected = tasks_mod.select(ids, tags)
    if args.all or (not ids and not tags):
        selected = tasks_mod.ALL_TASKS
    if args.limit:
        selected = selected[: args.limit]
    if not selected:
        print("没有匹配的任务。用 --list 看有哪些。")
        return 2

    pid, model, key, url = _resolve_provider(args)
    print(f"共 {len(selected)} 个任务\n")

    payload = harness.run_all(
        selected,
        out_path=args.out,
        provider_id=pid,
        model=model,
        api_key=key,
        base_url=url,
        keep=args.keep,
        verbose=args.verbose,
        budget=args.budget,
    )

    s = payload["summary"]
    print("\n" + "=" * 66)
    print(f"汇总：通过 {s['passed']}/{s['tasks']}（{s['pass_rate']:.0%}）"
          f" · 平均 {s['avg_steps']} 步 · {s['total_tokens']} tokens"
          f" · {s['total_duration_s']}s · 压缩 {s['compactions']} 次")
    print("=" * 66)

    if args.baseline:
        if not os.path.exists(args.baseline):
            print(f"⚠️ 基线文件不存在：{args.baseline}")
        else:
            with open(args.baseline, encoding="utf-8") as f:
                _compare(json.load(f), payload)

    harness.cleanup_env()
    return 0 if s["passed"] == s["tasks"] else 1


if __name__ == "__main__":
    sys.exit(main())
