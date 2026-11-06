"""任务规划角色（Planner）。

## 本次改造：从「盲订计划」到「先看代码再规划」

原来 Planner 是**一次纯文本调用**：没有工具、不读代码，凭一句任务描述直接出计划。
然后这份计划被作为 system 消息下发给 executor，并强制要求
「请严格按此计划执行，不要重新 create_plan」。

对真实代码库这是反效果的：一个没看过你项目的模型给出的计划经常是错的
（比如「先重构 X」而 X 根本不存在），而 executor 还被禁止重新规划。

现在 Planner 通过 ``mini_agent`` 拿到一组**只读工具**（读文件 / 搜索 / 列符号 /
看项目信息），先探索再规划。仍然保持两个设计：

1. **只读** —— Planner 绝不改任何东西，这是它和 Executor 的边界。
2. **产出仍是 JSON 步骤列表** —— 上层（app.py → executor）的契约不变。

失败时一律降级为「单步计划 + error」，让调用方能退回单 Agent 模式。
"""

from __future__ import annotations

import json

import mini_agent

PLANNER_PROMPT = """你是一个任务规划专家。你会拿到一个代码相关的任务，需要拆解成可执行的步骤。

## 工作方式
你可以调用**只读工具**先了解项目，再给出计划。建议顺序：

1. `project_info` 看项目类型与顶层结构
2. 需要时用 `search_files` / `list_symbols` / `find_references` 定位相关代码
3. 用 `read_file` 精读关键文件（**只读你真正需要的**，不要通读整个项目）

探索控制在 2~4 步。**不要为了探索而探索** —— 简单任务看一眼就可以直接规划。

## 规划原则
1. **每步要具体**：说清楚做什么、涉及哪个文件。
2. **步骤数量**：一般 3-8 步；太简单的任务不需要规划。
3. **顺序合理**：前一步的输出是后一步的输入。
4. **包含验证**：最后一步通常是跑测试或验证结果。
5. **基于事实**：只依据工具返回的真实内容规划，不要假设不存在的文件或函数。

## 输出格式
探索完成后，**只输出 JSON**，不要有任何其他内容：

{
  "steps": [
    "步骤1：xxx",
    "步骤2：xxx"
  ]
}
"""


def _extract_json(content: str) -> dict | None:
    """从模型输出里抠出 JSON（容忍 ```json 包裹和前后废话）。"""
    if not content:
        return None

    text = content.strip()
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) >= 2:
            text = parts[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
    try:
        return json.loads(text)
    except Exception:                                        # noqa: BLE001
        pass

    # 兜底：截取第一个 { 到最后一个 }
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:                                    # noqa: BLE001
            return None
    return None


def create_plan(
    task: str,
    api_key: str = None,
    base_url: str = None,
    model: str = None,
    project_root: str = None,
) -> dict:
    """让 Planner 探索项目后生成任务计划，返回 ``{"steps": [...]}``。

    任何环节失败都降级为 ``{"steps": [task], "error": ...}`` —— 上层据此
    提示用户「规划失败，降级为单 Agent 模式」，而不是让整个任务挂掉。
    """
    user_content = f"任务：{task}"
    if project_root:
        user_content += f"\n\n工作目录：{project_root}"

    result = mini_agent.run_tool_loop(
        system_prompt=PLANNER_PROMPT,
        user_content=user_content,
        allowed_tools=mini_agent.READONLY_TOOLS,
        api_key=api_key,
        base_url=base_url,
        model=model,
    )

    if result.get("error") and not result.get("text"):
        return {"steps": [task], "error": f"规划失败：{result['error']}"}

    plan = _extract_json(result.get("text", ""))
    if not isinstance(plan, dict) or "steps" not in plan:
        return {"steps": [task], "error": "Planner 未返回 steps"}

    steps = plan.get("steps")
    if isinstance(steps, str):
        steps = [steps]
    if not isinstance(steps, list) or not steps:
        return {"steps": [task], "error": "Planner 返回的 steps 为空"}

    steps = [str(s).strip() for s in steps if str(s).strip()]
    if not steps:
        return {"steps": [task], "error": "Planner 返回的 steps 为空"}

    return {"steps": steps, "explored": len(result.get("tool_calls") or [])}
