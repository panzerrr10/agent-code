from utils import call_llm_with_retry, get_llm_client
from config_providers import get_provider, DEFAULT_PROVIDER


def _get_default_client():
    """按默认 provider 取客户端（进程内复用）。"""
    cfg = get_provider(DEFAULT_PROVIDER)
    if not cfg.get("api_key"):
        raise RuntimeError(
            f"默认 provider {DEFAULT_PROVIDER} 的 api_key 未配置，请检查 .env"
        )
    return get_llm_client(cfg["api_key"], cfg["base_url"])


REVIEWER_PROMPT = """你是一个结果审查专家。

## 你的职责
检查 Executor 完成的任务，判断是否达成目标。

## 审查原则
1. **任务是否完成**：用户的目标是否达成？
2. **结果是否正确**：内容有没有明显错误？
3. **有无遗漏**：是否漏了什么关键步骤？
4. **改进建议**：如果有问题，给出改进方向。

## 输出格式
严格输出 JSON：
{
  "passed": true/false,
  "reason": "判断理由",
  "suggestions": ["改进建议1", "改进建议2"]
}

不要输出其他内容，只输出 JSON。
"""


def review(
    task: str,
    result: str,
    evidence: str = "",
    api_key: str = None,
    base_url: str = None,
    model: str = None,
) -> dict:
    """让 Reviewer 检查执行结果。

    evidence: Executor 的工具调用摘要（改了哪些文件、跑了哪些命令、结果如何）。
    只传最终文本会让审查流于表面，因此把证据一起交给 Reviewer。
    """
    # 动态选择 client 和 model
    default_cfg = get_provider(DEFAULT_PROVIDER)
    if api_key:
        actual_client = get_llm_client(api_key, base_url or default_cfg["base_url"])
        actual_model = model or default_cfg["default_model"]
    else:
        actual_client = _get_default_client()
        actual_model = model or default_cfg["default_model"]

    user_content = f"任务：{task}\n\n执行结果（最终答复）：{result}"
    if evidence:
        user_content += f"\n\n执行证据（工具调用与结果摘要）：\n{evidence}"

    try:
        resp = call_llm_with_retry(
            actual_client,
            model=actual_model,
            messages=[
                {"role": "system", "content": REVIEWER_PROMPT},
                {"role": "user", "content": user_content},
            ],
            temperature=0,
        )
        content = resp.choices[0].message.content.strip()

        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()

        import json

        review_result = json.loads(content)
        if not isinstance(review_result, dict) or "passed" not in review_result:
            # 模型没按约定返回 → 视为审查未完成，绝不默认「通过」
            return {
                "passed": None,
                "reason": "审查未完成：模型未返回合法的 passed 字段",
                "suggestions": [],
                "error": "invalid_review_payload",
            }
        return review_result
    except Exception as e:
        # 审查失败必须显式暴露为「未完成」，而不是静默 passed=True
        return {
            "passed": None,
            "reason": f"审查未完成：{e}",
            "suggestions": [],
            "error": str(e),
        }
