"""结果审查角色（Reviewer）。

## 本次改造：从「LLM 给 LLM 盖章」到「能自己查证」

原来 Reviewer 是**一次纯文本调用**：没有工具、不能读文件、不能跑测试。
它的全部输入只有 executor **自述的最终答复** + 一段工具调用摘要字符串。

也就是说：executor 说「我改好了，测试通过」，Reviewer 只能选择相信。
这是典型的「LLM 给 LLM 盖章」，**它验证不了任何东西**。

现在 Reviewer 通过 ``mini_agent`` 拿到：

- **只读工具**：自己去看 executor 到底改了哪些文件、改成了什么样
  （`read_file` / `get_function_body` / `search_files` / `git_diff` 之外的只读工具）
- **``run_tests``**：唯一算得上「硬证据」的东西。

## 关于 run_tests 与权限

Reviewer 是无人值守的小循环，**没有确认通道**（不可能弹卡片等你点勾），
而 run_tests 属于 execute 档。这里的处理是：

- 等级 1（只读）：**不给** run_tests，明确要求做静态审查
- 等级 ≥2：只有 policy 的 ``allow_reviewer_tests`` 打开时才给（默认开）
- 关掉那个开关 → 回到严格语义：评审只做静态审查

结论里会带 ``tests_run`` 字段，界面可以据此显示「本次评审是否真的跑了测试」，
避免用户误以为「评审通过」= 「测试通过」。
"""

from __future__ import annotations

import mini_agent
from utils import extract_json as _extract_json

REVIEWER_PROMPT = """你是一个结果审查专家。你需要**自己动手核实** Executor 是否真的完成了任务。

## 工作方式（重要）
不要只读 Executor 的自述 —— 那是它自己的说法。请用工具查证：

1. `project_info` / `list_symbols` 确认相关文件确实存在且结构合理
2. `read_file` / `get_function_body` **实际读一遍改动过的代码**，看逻辑对不对
3. `search_files` 确认该改的地方都改了、没有遗留旧写法
4. 如果可以，用 `run_tests` **跑一遍测试** —— 这是唯一的硬证据

查证控制在 2~5 步。查不到就直接说明「无法核实」，**不要猜**。

## 审查原则
1. **任务是否完成**：用户的目标是否达成？
2. **结果是否正确**：实际读到的代码有没有明显错误、边界问题？
3. **有无遗漏**：是否漏了关键步骤？
4. **诚实**：拿不到证据就说拿不到；**禁止**把「Executor 说通过了」当成「验证通过」。

## 输出格式
查证完成后，**只输出 JSON**，不要有任何其他内容：

{
  "passed": true,
  "reason": "判断理由（要引用你实际看到的内容）",
  "suggestions": ["改进建议1", "改进建议2"]
}

如果信息不足以下结论，`passed` 输出 `null`（不要默认 true）。
"""


def _test_tool_available() -> bool:
    """Reviewer 这次能不能拿到 run_tests（与 mini_agent 的判定保持一致）。"""
    import runtime

    try:
        if runtime.get_permission_level() <= 1:
            return False
    except Exception:                                        # noqa: BLE001
        return False
    try:
        import policy

        return policy.is_enabled("allow_reviewer_tests")
    except Exception:                                        # noqa: BLE001
        return False


def review(
    task: str,
    result: str,
    evidence: str = "",
    api_key: str = None,
    base_url: str = None,
    model: str = None,
    project_root: str = None,
) -> dict:
    """审查执行结果。

    ``evidence``：Executor 的工具调用摘要。仍然传给 Reviewer —— 它是定位线索
    （「它说改了 a.py」），但**不再是唯一依据**：Reviewer 现在能自己去读、去跑。

    返回 ``{"passed", "reason", "suggestions", "tests_run", "tool_calls", "error"}``。
    ``passed`` 可能是 ``None``（无法判定），**不要**把它当成通过。
    """
    allowed = list(mini_agent.READONLY_TOOLS)
    can_test = _test_tool_available()
    if can_test:
        allowed.append(mini_agent.TEST_TOOL)

    user_content = f"任务：{task}\n\nExecutor 的自述结果（**未经核实，仅供参考**）：\n{result}"
    if evidence:
        user_content += f"\n\nExecutor 的工具调用线索：\n{evidence}"
    if project_root:
        user_content += f"\n\n工作目录：{project_root}"
    if can_test:
        user_content += "\n\n你有 `run_tests` 可用，请务必跑一次测试再下结论。"
    else:
        user_content += (
            "\n\n**你没有跑测试的权限/开关**，请在 reason 里明确写「未运行测试，"
            "结论基于静态审查」，不要暗示测试通过了。"
        )

    loop = mini_agent.run_tool_loop(
        system_prompt=REVIEWER_PROMPT,
        user_content=user_content,
        allowed_tools=allowed,
        api_key=api_key,
        base_url=base_url,
        model=model,
    )

    data = _extract_json(loop.get("text", ""))
    if not isinstance(data, dict):
        # 解析不出 JSON 时**绝不能默认通过** —— 返回 passed=None 让上层如实展示
        if loop.get("error") and not loop.get("text"):
            reason = f"审查未完成：{loop['error']}"
        else:
            reason = "审查结果不是合法 JSON，无法判定"
        return {
            "passed": None,
            "reason": reason,
            "suggestions": [],
            "tests_run": _did_run_tests(loop),
            "tool_calls": loop.get("tool_calls") or [],
            "error": loop.get("error") or "输出解析失败",
        }

    passed = data.get("passed")
    if not isinstance(passed, bool):
        passed = None

    return {
        "passed": passed,
        "reason": str(data.get("reason") or "").strip(),
        "suggestions": [str(s) for s in (data.get("suggestions") or [])][:8],
        "tests_run": _did_run_tests(loop),
        "tool_calls": loop.get("tool_calls") or [],
        "error": loop.get("error"),
    }


def _did_run_tests(loop: dict) -> bool:
    """本次审查是否真的执行过测试（供界面显示，避免「评审通过」被误读为「测试通过」）。"""
    return any(tc.get("name") == mini_agent.TEST_TOOL for tc in (loop.get("tool_calls") or []))
