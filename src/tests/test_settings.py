"""设置面板后端：provider 密钥/URL 存储 与 工作区打开记录。

**最要紧的一条**：任何接口响应里都不能出现完整密钥。
密钥只存服务端（data/providers.json，权限 600），前端只拿掩码。
"""

import json
import os
import re
import sys

import pytest

sys.path.insert(0, "src")

import config_providers as cp  # noqa: E402
import db  # noqa: E402

SECRET = "sk-supersecret-abcdefghijklmnop123456"


EMPTY_CFG = {"providers": {}, "default_provider": "", "rag_provider": ""}


@pytest.fixture(autouse=True)
def _clean_settings():
    """每个用例前把 provider 配置重置成**显式空**。

    注意是「写入空配置」而不是「删掉文件」：文件不存在时
    ``load_config()`` 会触发一次 .env 迁移，于是测试里会凭空冒出
    真实 .env 里那几家 provider —— 测出来的东西就不是本用例加的了。
    """
    os.makedirs(os.path.dirname(cp.SETTINGS_FILE), exist_ok=True)
    with open(cp.SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(EMPTY_CFG, f)
    yield
    with open(cp.SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(EMPTY_CFG, f)


# ============ 提供商管理（用户自定义，后端无预设）============


def test_settings_file_is_isolated_from_real_data():
    """conftest 必须把设置文件指到临时目录，否则会写进真实 data/。"""
    assert "agentcode-test-" in cp.SETTINGS_FILE, cp.SETTINGS_FILE


def _add(client, **kw):
    body = {"name": "测试家", "base_url": "https://x.example/v1",
            "api_key": SECRET, "models": ["m-1", "m-2"]}
    body.update(kw)
    return client.post("/api/providers", json=body)


def test_add_provider_writes_file_with_600(client):
    r = _add(client)
    assert r.status_code == 200 and r.get_json()["ok"]
    assert os.path.exists(cp.SETTINGS_FILE)
    assert oct(os.stat(cp.SETTINGS_FILE).st_mode & 0o777) == "0o600", "密钥文件必须仅本人可读写"
    saved = json.load(open(cp.SETTINGS_FILE, encoding="utf-8"))
    one = next(iter(saved["providers"].values()))
    assert one["api_key"] == SECRET
    assert one["base_url"] == "https://x.example/v1"
    assert one["models"] == ["m-1", "m-2"]
    assert one["default_model"] == "m-1", "默认模型应自动取第一个"


def test_response_never_contains_full_key(client):
    """**最关键的断言**：任何接口响应里都不允许出现完整密钥。"""
    _add(client)
    for url in ("/api/providers",):
        body = client.get(url).get_data(as_text=True)
        assert SECRET not in body, f"{url} 泄漏了完整密钥"
    # 写接口的响应同样不能带
    body = _add(client, name="第二家", id="second").get_data(as_text=True)
    assert SECRET not in body, "写接口响应泄漏了完整密钥"
    # 删除接口的响应也不能带
    pid = "second"
    body = client.delete(f"/api/providers/{pid}").get_data(as_text=True)
    assert SECRET not in body, "删除接口响应泄漏了完整密钥"


def test_providers_only_exposes_mask(client):
    pid = _add(client).get_json()["providers"][0]["id"]
    p = next(x for x in client.get("/api/providers").get_json()["providers"] if x["id"] == pid)
    assert p["has_key"] is True
    assert p["key_hint"] and SECRET not in p["key_hint"]
    assert p["key_hint"].startswith("sk-") and p["key_hint"].endswith(SECRET[-4:])


def test_edit_without_key_keeps_old_key(client):
    """界面上永远不回显密钥，所以「留空」必须是「不改」而不是「清空」。"""
    pid = _add(client).get_json()["providers"][0]["id"]
    r = client.post("/api/providers", json={"id": pid, "name": "改了名",
                                            "base_url": "https://y.example/v1",
                                            "models": ["m-9"]})
    assert r.get_json()["ok"]
    saved = json.load(open(cp.SETTINGS_FILE, encoding="utf-8"))["providers"][pid]
    assert saved["api_key"] == SECRET, "只改名称/URL 不该把密钥弄丢"
    assert saved["name"] == "改了名"
    assert saved["base_url"] == "https://y.example/v1"


def test_can_replace_key_explicitly(client):
    pid = _add(client).get_json()["providers"][0]["id"]
    client.post("/api/providers", json={"id": pid, "name": "n", "base_url": "https://x.example/v1",
                                        "models": ["m"], "api_key": "sk-new-key-000000000000"})
    saved = json.load(open(cp.SETTINGS_FILE, encoding="utf-8"))["providers"][pid]
    assert saved["api_key"] == "sk-new-key-000000000000"


def test_delete_provider(client):
    pid = _add(client).get_json()["providers"][0]["id"]
    r = client.delete(f"/api/providers/{pid}").get_json()
    assert r["ok"] and r["providers"] == []
    assert client.delete("/api/providers/nope").status_code == 404


def test_delete_provider_fixes_default_and_rag(client):
    """删掉正被 default / rag 指向的那家时，要兜底改指，不能留下悬空 id。"""
    a = _add(client, name="A").get_json()["providers"][0]["id"]
    b = _add(client, name="B").get_json()["providers"][1]["id"]
    client.post("/api/providers/default", json={"id": a})
    client.post("/api/providers/rag", json={"id": a})
    client.delete(f"/api/providers/{a}")
    j = client.get("/api/providers").get_json()
    assert j["default"] == b
    assert j["rag_provider"] in ("", b)


def test_default_and_rag_selectors(client):
    pid = _add(client).get_json()["providers"][0]["id"]
    assert client.post("/api/providers/default", json={"id": pid}).get_json()["default"] == pid
    assert client.post("/api/providers/rag", json={"id": pid}).get_json()["rag_provider"] == pid
    assert client.post("/api/providers/default", json={"id": "nope"}).status_code == 404


def test_no_presets_in_code():
    """**代码里不允许再有预设提供商** —— 这正是这次重做的目的。"""
    import inspect

    src = inspect.getsource(cp)
    assert "PROVIDERS = {" not in src, "config_providers 里还有写死的 PROVIDERS 常量"
    assert "DEFAULT_PROVIDER = " not in src, "还有写死的默认 provider"
    assert "RAG_PROVIDER = " not in src, "还有写死的 RAG provider"


def test_empty_config_gives_empty_list(client):
    assert client.get("/api/providers").get_json()["providers"] == []


def test_unknown_provider_falls_back_to_default(client):
    """传一个不存在的 provider_id 不该崩，应回退到默认。"""
    pid = _add(client).get_json()["providers"][0]["id"]
    assert cp.get_provider("nope")["id"] == pid


def test_get_provider_returns_copy(client):
    """必须返回副本，否则调用方一改就污染配置文件。"""
    pid = _add(client).get_json()["providers"][0]["id"]
    a = cp.get_provider(pid)
    a["api_key"] = "MUTATED"
    assert cp.get_provider(pid)["api_key"] == SECRET


def test_placeholder_keys_are_not_configured(client):
    _add(client, api_key="你的密钥")
    p = client.get("/api/providers").get_json()["providers"][0]
    assert p["has_key"] is False


@pytest.mark.parametrize("bad", ["ftp://x", "javascript:alert(1)", "x.example.com", "//x"])
def test_rejects_non_http_base_url(client, bad):
    r = _add(client, base_url=bad)
    assert r.status_code == 400
    saved = json.load(open(cp.SETTINGS_FILE, encoding="utf-8"))
    assert saved["providers"] == {}, "非法输入不该落盘"


def test_rejects_incomplete_input(client):
    assert client.post("/api/providers", json={"base_url": "https://x/v1"}).status_code == 400, "没有名称"
    assert client.post("/api/providers", json={"name": "n"}).status_code == 400, "没有 URL"
    assert client.post("/api/providers",
                       json={"name": "n", "base_url": "https://x/v1"}).status_code == 400, "没有模型"


def test_api_key_is_optional_for_local_models(client):
    """本地模型（Ollama / LM Studio）根本不需要 key，不该强制填。"""
    r = client.post("/api/providers",
                    json={"name": "本地", "base_url": "http://127.0.0.1:11434/v1",
                          "models": ["llama3"]})
    assert r.status_code == 200, r.get_json()
    p = r.get_json()["providers"][0]
    assert p["has_key"] is False
    assert p["models"] == ["llama3"]


def test_same_id_upserts_instead_of_duplicating(client):
    """同一个 id 提交两次 = 更新，不会变成两条。

    （接口是 upsert：id 已存在就更新，不存在就按这个 id 新增。
    否则用户就没法用一个自己指定的 id 建提供商。）
    """
    _add(client, id="dup", name="第一次")
    r = _add(client, id="dup", name="第二次")
    assert r.status_code == 200
    names = [p["name"] for p in r.get_json()["providers"]]
    assert names == ["第二次"], f"不该出现两条：{names}"


def test_id_auto_generated_from_name(client):
    pid = _add(client, name="My Provider", id=None).get_json()["providers"][0]["id"]
    assert pid == "my-provider"


def test_models_accept_comma_and_newline(client):
    pid = _add(client, models="a, b\nc ,, a").get_json()["providers"][0]["id"]
    p = cp.get_provider(pid)
    assert p["models"] == ["a", "b", "c"], "应支持逗号/换行分隔并去重"


def test_providers_require_auth(anon_client):
    assert anon_client.get("/api/providers").status_code == 401
    assert anon_client.post("/api/providers", json={"name": "x"}).status_code == 401
    assert anon_client.delete("/api/providers/x").status_code == 401
    assert anon_client.post("/api/providers/default", json={"id": "x"}).status_code == 401
    assert anon_client.post("/api/providers/test", json={"id": "x"}).status_code == 401


def test_no_key_like_string_in_providers_payload(client):
    """配置的密钥不能以任何形式出现在响应里（含 .env 迁移进来的）。"""
    _add(client)
    blob = client.get("/api/providers").get_data(as_text=True)
    assert SECRET not in blob
    assert not re.search(r"sk-[A-Za-z0-9]{20,}", blob), "响应里出现了完整密钥"


def test_legacy_env_is_migrated_once(monkeypatch):
    """升级兼容：老用户的可跑配置全在 .env 里，不能一升级就不能用。

    只有**确实配了可用 key** 的才迁移（占位符不算），
    迁移后它们就是普通的用户提供商，随时可改可删。
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-real-deepseek-key-000")
    monkeypatch.setenv("ZHIPU_API_KEY", "你的智谱密钥")     # 占位符
    if os.path.exists(cp.SETTINGS_FILE):
        os.remove(cp.SETTINGS_FILE)                          # 模拟旧安装：没有配置文件

    providers = cp.get_provider_list()
    ids = [p["id"] for p in providers]
    assert "deepseek" in ids, "配了 key 的应该被迁移"
    assert "zhipu" not in ids, "占了位符的不该迁移"
    assert cp.get_default_provider_id() == "deepseek"
    assert cp.get_provider("deepseek")["api_key"] == "sk-real-deepseek-key-000"
    assert os.path.exists(cp.SETTINGS_FILE), "迁移结果要落盘，避免每次读都重算"


def test_migration_does_not_run_twice(monkeypatch):
    """已经迁移过（有 providers 键）就不再动它 —— 用户删掉的不要又冒出来。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-real-deepseek-key-000")
    cp.get_provider_list()                                    # 触发迁移
    cp.delete_provider("deepseek")
    assert cp.get_provider_list() == [], "用户删掉的提供商不该被再次迁移出来"


# ============ 工作区打开记录 ============


def test_open_files_roundtrip(client, tmp_path):
    d = os.path.join(os.path.expanduser("~"), "下载")
    wid = client.post("/workspaces", json={"name": "of", "path": d}).get_json()["id"]
    assert client.get(f"/workspaces/{wid}/open_files").get_json()["paths"] == []

    paths = [os.path.join(d, "a.py"), os.path.join(d, "b.md")]
    assert client.put(f"/workspaces/{wid}/open_files", json={"paths": paths}).get_json()["count"] == 2
    assert client.get(f"/workspaces/{wid}/open_files").get_json()["paths"] == paths


def test_open_files_filters_disallowed(client):
    d = os.path.join(os.path.expanduser("~"), "下载")
    wid = client.post("/workspaces", json={"name": "of2", "path": d}).get_json()["id"]
    r = client.put(f"/workspaces/{wid}/open_files",
                   json={"paths": [os.path.join(d, "ok.py"), "/etc/passwd"]}).get_json()
    assert r["count"] == 1
    assert client.get(f"/workspaces/{wid}/open_files").get_json()["paths"] == [os.path.join(d, "ok.py")]


def test_open_files_isolated_per_workspace(client):
    d = os.path.join(os.path.expanduser("~"), "下载")
    a = client.post("/workspaces", json={"name": "wsA", "path": d}).get_json()["id"]
    b = client.post("/workspaces", json={"name": "wsB", "path": d}).get_json()["id"]
    client.put(f"/workspaces/{a}/open_files", json={"paths": [os.path.join(d, "only-a.py")]})
    assert client.get(f"/workspaces/{b}/open_files").get_json()["paths"] == [], "工作区之间必须隔离"


def test_open_files_rejects_bad_input(client):
    d = os.path.join(os.path.expanduser("~"), "下载")
    wid = client.post("/workspaces", json={"name": "of3", "path": d}).get_json()["id"]
    assert client.put(f"/workspaces/{wid}/open_files", json={"paths": "x"}).status_code == 400
    assert client.get("/workspaces/999999/open_files").status_code == 404


def test_open_files_caps_list_length(client, monkeypatch):
    d = os.path.join(os.path.expanduser("~"), "下载")
    wid = client.post("/workspaces", json={"name": "of4", "path": d}).get_json()["id"]
    many = [os.path.join(d, f"f{i}.py") for i in range(200)]
    client.put(f"/workspaces/{wid}/open_files", json={"paths": many})
    assert len(db.get_workspace_open_files(wid)) <= 50, "记录不能无限增长"


def test_open_files_survives_reconnect(client):
    """模拟重启：直接绕过内存去库里读。"""
    d = os.path.join(os.path.expanduser("~"), "下载")
    wid = client.post("/workspaces", json={"name": "of5", "path": d}).get_json()["id"]
    client.put(f"/workspaces/{wid}/open_files", json={"paths": [os.path.join(d, "x.py")]})
    assert db.get_workspace_open_files(wid) == [os.path.join(d, "x.py")]
