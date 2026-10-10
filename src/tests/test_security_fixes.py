"""P0 / P1 修复的回归测试：安全边界与关键正确性。

覆盖：
- 路径白名单（is_path_permitted / 敏感目录拒绝 / project_root 放行）
- 多 provider 额外参数条件化（DeepSeek 专属 enable_search 不外发）
- 模型名不再是占位符
- git 参数不走 shell（命令注入防护）
- multi_edit 真正原子（预检失败整体放弃、同文件多处编辑）
- Reviewer 失败语义（不再静默 passed=True）
- /execute 端点已下线、文件预览接口拒绝敏感路径
- 示例文件不再含真实密钥
"""

import json
import os
import shutil
import types

import pytest

import reviewer
import runtime
import tools
from config_providers import get_extra_body, get_provider_id_by_base_url
from paths import is_allowed_path, is_path_permitted

HOME = os.path.expanduser("~")
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _clean_runtime_state():
    """每个用例前后清空粘性状态，避免会话状态跨用例串味。"""
    runtime.reset_all()
    runtime.set_session_id(None)
    yield
    runtime.reset_all()
    runtime.set_session_id(None)


# ============ 路径白名单 ============


def test_is_path_permitted_rejects_sensitive_dirs():
    assert is_path_permitted(os.path.join(HOME, ".ssh", "id_rsa")) is False
    assert is_path_permitted(os.path.join(HOME, ".aws", "credentials")) is False


def test_is_path_permitted_allows_project_root_outside_home(tmp_path):
    # /tmp/... 不在 home 下：is_allowed_path 拒绝，但显式选择为 project_root 后放行
    runtime.set_session_id("test-session")
    runtime.set_project_root(str(tmp_path))
    target = str(tmp_path / "sub" / "a.py")
    assert is_allowed_path(target) is False
    assert is_path_permitted(target) is True


def test_is_path_permitted_still_rejects_sensitive_inside_root():
    # 即使 project_root 在 home 内，敏感目录依然拒绝
    runtime.set_session_id("test-session")
    runtime.set_project_root(HOME)
    assert is_path_permitted(os.path.join(HOME, ".ssh", "id_rsa")) is False


# ============ 多 provider 参数条件化 ============



def test_provider_id_inferred_from_base_url(client):
    """按 base_url 反查 provider —— 用户自定义提供商时 id 是随便起的，
    所以这个反查只能靠 URL 匹配，不能靠写死的 id 表。"""
    import config_providers as cp

    cp.add_provider({"id": "openai", "name": "OpenAI",
                     "base_url": "https://api.openai.com/v1", "models": ["gpt-4o"]})
    cp.add_provider({"id": "bailian", "name": "百炼",
                     "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                     "models": ["qwen-plus"]})
    cp.add_provider({"id": "deepseek", "name": "DeepSeek",
                     "base_url": "https://api.deepseek.com/v1", "models": ["deepseek-chat"]})

    assert get_provider_id_by_base_url("https://api.openai.com/v1") == "openai"
    assert get_provider_id_by_base_url(
        "https://dashscope.aliyuncs.com/compatible-mode/v1") == "bailian"
    assert get_provider_id_by_base_url("https://api.deepseek.com/v1") == "deepseek"
    # 认不出来时退到默认（第一个），绝不猜
    assert get_provider_id_by_base_url("") == cp.get_default_provider_id()
    assert get_provider_id_by_base_url("https://who.example/v1") == cp.get_default_provider_id()


def test_extra_body_only_for_deepseek(client):
    """只有 DeepSeek 认 enable_search，别家收到会 400 —— 必须条件化下发。

    **按 base_url 判断而不是按 id**：用户可以把提供商叫任何名字。
    """
    import config_providers as cp

    cp.add_provider({"id": "whatever", "name": "随便叫",
                     "base_url": "https://api.deepseek.com/v1", "models": ["m"]})
    cp.add_provider({"id": "other", "name": "别家",
                     "base_url": "https://api.moonshot.cn/v1", "models": ["m"]})
    assert get_extra_body("whatever") == {
        "enable_search": True, "search_options": {"search_strategy": "turbo"}}
    assert get_extra_body("other") == {}, "别家绝不能收到 enable_search"


def test_model_names_are_not_placeholders():
    """内置的旧预设只能用于一次性迁移，里面的模型名必须是真实可用的。"""
    import config_providers as cp

    for pid, p in cp._LEGACY_PRESETS.items():
        assert p["models"], f"{pid} 模型列表为空"
        assert p["default_model"] in p["models"], f"{pid} 默认模型不在列表内"
        for name in p["models"]:
            assert "2026" not in name
            assert "placeholder" not in name.lower()
            assert name not in ("deepseek-flash", "deepseek-v4-pro")


# ============ 命令注入防护 ============


def test_git_rollback_rejects_injection(tmp_path):
    marker = tmp_path / "pwned"
    payload = f'HEAD~1"; touch {marker}; "'
    result = tools.git_rollback(payload)
    assert "非法" in result
    assert not marker.exists()


def test_git_log_rejects_non_integer():
    assert "整数" in tools.git_log("1; rm -rf /")  # type: ignore[arg-type]


@pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")
def test_git_commit_message_with_metacharacters_is_not_executed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=repo, check=True)
    (repo / "a.txt").write_text("hi", encoding="utf-8")

    runtime.set_session_id("test-session")
    runtime.set_project_root(str(repo))
    marker = tmp_path / "pwned_commit"
    message = f'x"; touch {marker}; echo "'
    tools.git_commit(message)
    assert not marker.exists()


# ============ multi_edit 原子性 ============


def test_multi_edit_aborts_entirely_on_any_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "is_allowed_path", lambda p: True)
    runtime.set_session_id("test-session")
    runtime.set_project_root(str(tmp_path))

    f1 = tmp_path / "f1.txt"
    f2 = tmp_path / "f2.txt"
    f1.write_text("AAA", encoding="utf-8")
    f2.write_text("BBB", encoding="utf-8")

    result = tools.multi_edit(
        [
            {"path": str(f1), "old_text": "AAA", "new_text": "XXX"},
            {"path": str(f2), "old_text": "NOT_THERE", "new_text": "YYY"},
        ]
    )
    assert "已放弃" in result
    # 第 1 项虽合法，也必须保持原样（不落盘）
    assert f1.read_text(encoding="utf-8") == "AAA"
    assert f2.read_text(encoding="utf-8") == "BBB"


def test_multi_edit_supports_multiple_edits_on_same_file(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "is_allowed_path", lambda p: True)
    runtime.set_session_id("test-session")
    runtime.set_project_root(str(tmp_path))

    f1 = tmp_path / "f1.txt"
    f2 = tmp_path / "f2.txt"
    f1.write_text("AAA", encoding="utf-8")
    f2.write_text("BBB", encoding="utf-8")

    result = tools.multi_edit(
        [
            {"path": str(f1), "old_text": "AAA", "new_text": "A1"},
            {"path": str(f1), "old_text": "A1", "new_text": "A2"},
            {"path": str(f2), "old_text": "BBB", "new_text": "B1"},
        ]
    )
    assert "成功" in result
    assert f1.read_text(encoding="utf-8") == "A2"
    assert f2.read_text(encoding="utf-8") == "B1"


# ============ Reviewer 语义 ============


def _fake_resp(content: str):
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content))]
    )


def test_reviewer_exception_is_not_passed(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(reviewer, "call_llm_with_retry", boom)
    result = reviewer.review("task", "result", api_key="test-key")
    assert result["passed"] is None
    assert "审查未完成" in result["reason"]


def test_reviewer_invalid_payload_is_not_passed(monkeypatch):
    monkeypatch.setattr(
        reviewer, "call_llm_with_retry", lambda *a, **k: _fake_resp("这不是 JSON")
    )
    result = reviewer.review("task", "result", api_key="test-key")
    assert result["passed"] is None


def test_reviewer_preserves_failed_verdict(monkeypatch):
    payload = {"passed": False, "reason": "没改对", "suggestions": ["重做"]}
    monkeypatch.setattr(
        reviewer,
        "call_llm_with_retry",
        lambda *a, **k: _fake_resp(json.dumps(payload)),
    )
    result = reviewer.review(
        "task", "result", evidence="- 调用 edit_file(...)", api_key="test-key"
    )
    assert result["passed"] is False
    assert result["suggestions"] == ["重做"]


# ============ 端点与密钥 ============


def _auth_client():
    """带鉴权头的测试客户端（服务端已强制 X-Auth-Token）。"""
    import app as app_module
    import auth

    c = app_module.app.test_client()
    c.environ_base["HTTP_X_AUTH_TOKEN"] = auth.get_token()
    return c


def test_execute_endpoint_is_gone():
    client = _auth_client()
    resp = client.post(
        "/execute",
        json={"session_id": 1, "tool_name": "list_dir", "tool_args": {}},
    )
    assert resp.status_code == 404


def test_file_content_rejects_sensitive_path():
    client = _auth_client()
    resp = client.get(
        "/api/file_content", query_string={"path": os.path.join(HOME, ".ssh", "id_rsa")}
    )
    # 权限校验先于存在性判断：即使文件不存在也返回 403，不泄露路径是否存在
    assert resp.status_code == 403


def test_example_files_contain_no_real_secrets():
    """示例文件只允许占位符，不允许出现长格式的真实密钥。

    注意：这两个文件一个在项目根、一个在 docs/，必须用显式路径定位——
    历史上这里用「往上找两级」推导过路径，目录一调整就会静默跳过、断言形同虚设。
    """
    import re

    import paths

    secret_re = re.compile(r"sk-[A-Za-z0-9._\-]{40,}|[0-9a-f]{32}\.[A-Za-z0-9]{10,}")
    candidates = [
        os.path.join(paths.BASE_DIR, ".env.example"),
        os.path.join(paths.BASE_DIR, "docs", "备忘录.txt"),
    ]
    checked = 0
    for path in candidates:
        if not os.path.exists(path):
            continue
        checked += 1
        text = open(path, encoding="utf-8", errors="ignore").read()
        hits = secret_re.findall(text)
        assert not hits, f"{os.path.basename(path)} 疑似仍含真实密钥"
    assert checked > 0, "一个待检查的示例文件都没找到，说明路径定位又失效了"
