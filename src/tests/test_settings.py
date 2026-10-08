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


@pytest.fixture(autouse=True)
def _clean_settings():
    """每个用例前清掉界面里保存的 provider 配置。"""
    if os.path.exists(cp.SETTINGS_FILE):
        os.remove(cp.SETTINGS_FILE)
    yield
    if os.path.exists(cp.SETTINGS_FILE):
        os.remove(cp.SETTINGS_FILE)


# ============ 密钥读写 ============


def test_settings_file_is_isolated_from_real_data():
    """conftest 必须把设置文件指到临时目录，否则会写进真实 data/。"""
    assert "agentcode-test-" in cp.SETTINGS_FILE, cp.SETTINGS_FILE


def test_save_key_writes_file_with_600(client):
    r = client.post("/api/settings/keys",
                    json={"provider_id": "zhipu", "api_key": SECRET, "base_url": "https://x.example/v1"})
    assert r.status_code == 200 and r.get_json()["ok"]
    assert os.path.exists(cp.SETTINGS_FILE)
    assert oct(os.stat(cp.SETTINGS_FILE).st_mode & 0o777) == "0o600", "密钥文件必须仅本人可读写"
    saved = json.load(open(cp.SETTINGS_FILE, encoding="utf-8"))
    assert saved["zhipu"]["api_key"] == SECRET
    assert saved["zhipu"]["base_url"] == "https://x.example/v1"


def test_response_never_contains_full_key(client):
    """最关键的断言：接口响应里不允许出现完整密钥。"""
    client.post("/api/settings/keys", json={"provider_id": "zhipu", "api_key": SECRET})
    for url in ("/api/providers", "/api/settings/keys"):
        body = client.get(url).get_data(as_text=True)
        assert SECRET not in body, f"{url} 泄漏了完整密钥"
    # 写接口的响应同样不能带
    body = client.post("/api/settings/keys",
                       json={"provider_id": "openai", "api_key": SECRET}).get_data(as_text=True)
    assert SECRET not in body, "写接口响应泄漏了完整密钥"


def test_settings_only_exposes_mask(client):
    client.post("/api/settings/keys", json={"provider_id": "zhipu", "api_key": SECRET})
    p = next(x for x in client.get("/api/providers").get_json()["providers"] if x["id"] == "zhipu")
    assert p["has_key"] is True
    assert p["key_hint"] and SECRET not in p["key_hint"]
    assert p["key_hint"].startswith("sk-") and p["key_hint"].endswith(SECRET[-4:])
    assert p["from_settings"] is True


def test_ui_key_takes_priority_over_env(client, monkeypatch):
    """界面里填的优先于 .env。"""
    monkeypatch.setenv("ZHIPU_API_KEY", "env-key-0000000000")
    client.post("/api/settings/keys", json={"provider_id": "zhipu", "api_key": "ui-key-1111111111"})
    assert cp.get_provider("zhipu")["api_key"] == "ui-key-1111111111"


def test_env_used_when_no_ui_key(monkeypatch):
    monkeypatch.setitem(cp.PROVIDERS, "zhipu", dict(cp.PROVIDERS["zhipu"], api_key="env-only-key"))
    assert cp.get_provider("zhipu")["api_key"] == "env-only-key"


def test_clear_key_falls_back(client):
    client.post("/api/settings/keys", json={"provider_id": "zhipu", "api_key": SECRET})
    r = client.delete("/api/settings/keys/zhipu").get_json()
    assert r["ok"]
    p = next(x for x in r["providers"] if x["id"] == "zhipu")
    assert p["from_settings"] is False


def test_get_provider_returns_copy():
    """必须返回副本，否则调用方一改就污染全局 PROVIDERS。"""
    a = cp.get_provider("deepseek")
    a["api_key"] = "MUTATED"
    assert cp.PROVIDERS["deepseek"]["api_key"] != "MUTATED"


def test_placeholder_keys_are_not_configured(monkeypatch):
    monkeypatch.setitem(cp.PROVIDERS, "zhipu", dict(cp.PROVIDERS["zhipu"], api_key="你的智谱密钥"))
    p = next(x for x in cp.get_provider_list() if x["id"] == "zhipu")
    assert p["has_key"] is False


@pytest.mark.parametrize("bad", ["ftp://x", "javascript:alert(1)", "x.example.com", "//x"])
def test_rejects_non_http_base_url(client, bad):
    r = client.post("/api/settings/keys", json={"provider_id": "zhipu", "base_url": bad})
    assert r.status_code == 400
    assert not os.path.exists(cp.SETTINGS_FILE), "非法输入不该落盘"


def test_rejects_unknown_provider(client):
    assert client.post("/api/settings/keys",
                       json={"provider_id": "nope", "api_key": "x"}).status_code == 400
    assert client.delete("/api/settings/keys/nope").status_code == 400


def test_base_url_only_update_keeps_key(client):
    client.post("/api/settings/keys", json={"provider_id": "zhipu", "api_key": SECRET})
    client.post("/api/settings/keys", json={"provider_id": "zhipu", "base_url": "https://y.example/v1"})
    saved = json.load(open(cp.SETTINGS_FILE, encoding="utf-8"))["zhipu"]
    assert saved["api_key"] == SECRET, "只改 URL 不该把密钥弄丢"
    assert saved["base_url"] == "https://y.example/v1"


def test_settings_requires_auth(anon_client):
    assert anon_client.post("/api/settings/keys", json={"provider_id": "zhipu"}).status_code == 401
    assert anon_client.delete("/api/settings/keys/zhipu").status_code == 401


def test_no_key_like_string_in_providers_payload(client):
    """.env 里的真实密钥也不能出现在响应里。"""
    from config_providers import PROVIDERS

    blob = client.get("/api/providers").get_data(as_text=True)
    for pid, p in PROVIDERS.items():
        key = p.get("api_key") or ""
        if len(key) > 12:
            assert key not in blob, f"{pid} 的密钥出现在 /api/providers 响应里"
    assert not re.search(r"sk-[A-Za-z0-9]{20,}", blob)


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
