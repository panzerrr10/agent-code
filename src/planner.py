from config_providers import DEFAULT_PROVIDER, get_provider
from utils import call_llm_with_retry, get_llm_client


def _get_default_client():
    """按默认 provider 取客户端（进程内复用）。"""
    cfg = get_provider(DEFAULT_PROVIDER)
    if not cfg.get("api_key"):
        raise RuntimeError(
            f"默认 provider {DEFAULT_PROVIDER} 的 api_key 未配置，请检查 .env"
        )
    return get_llm_client(cfg["api_key"], cfg["base_url"])


PLANNER_PROMPT = """你是一个任务规划专家。

## 你的职责
根据用户的复杂任务，拆解成清晰的、可执行的步骤列表。

## 规划原则
1. **每步要具体**：说清楚做什么、用什么工具。
2. **步骤数量**：一般 3-8 步，太简单不需要规划。
3. **顺序合理**：前一步的输出是后一步的输入。
4. **包含验证**：最后一步通常是测试或验证。

## 输出格式
严格输出 JSON，格式：
{
  "steps": [
    "步骤1：xxx",
    "步骤2：xxx",
    ...
  ]
}

不要输出其他内容，只输出 JSON。
"""


def create_plan(
    task: str, api_key: str = None, base_url: str = None, model: str = None
) -> dict:
    """让 Planner 生成任务计划，返回 {"steps": [...]}"""
    # 动态选择 client 和 model
    default_cfg = get_provider(DEFAULT_PROVIDER)
    if api_key:
        actual_client = get_llm_client(api_key, base_url or default_cfg["base_url"])
        actual_model = model or default_cfg["default_model"]
    else:
        actual_client = _get_default_client()
        actual_model = model or default_cfg["default_model"]

    try:
        resp = call_llm_with_retry(
            actual_client,
            model=actual_model,
            messages=[
                {"role": "system", "content": PLANNER_PROMPT},
                {"role": "user", "content": f"任务：{task}"},
            ],
            temperature=0,
        )
        content = resp.choices[0].message.content.strip()

        # 去掉可能的 ```json ... ``` 包裹
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()

        import json

        plan = json.loads(content)
        if "steps" not in plan:
            return {"steps": [task], "error": "Planner 未返回 steps"}
        return plan
    except Exception as e:
        return {"steps": [task], "error": f"规划失败：{e}"}
