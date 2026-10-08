"""附件上传与下载的回归测试。

需求背景：回形针可以传文本文档或图片给 Agent 看；Agent 按要求改完文档后，
用户能把新文档下载下来（浏览器默认存到系统「下载」目录，本机即 /home/pz/下载）。
"""

import io
import os
import shutil
import sys

import pytest

sys.path.insert(0, "src")

import agent  # noqa: E402
import app as app_module  # noqa: E402
import paths  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_uploads():
    up = paths.UPLOAD_DIR
    before = set(os.listdir(up)) if os.path.isdir(up) else set()
    yield
    # 只清掉本次测试新建的会话目录，别动用户已有的
    if os.path.isdir(up):
        for name in set(os.listdir(up)) - before:
            shutil.rmtree(os.path.join(up, name), ignore_errors=True)


def _upload(client, content: bytes, filename: str, sid=None):
    return client.post(
        "/api/attach",
        data={"session_id": str(sid or ""), "file": (io.BytesIO(content), filename)},
        content_type="multipart/form-data",
    )


# ============ 上传 ============


def test_upload_text_document(client):
    sid = client.post("/sessions", json={}).get_json()["id"]
    r = _upload(client, "第一行\n第二行\n".encode(), "notes.md", sid)
    body = r.get_json()
    assert r.status_code == 200 and body["ok"]
    assert body["name"] == "notes.md"
    assert body["chars"] == 8   # "第一行\n第二行\n"
    assert os.path.isfile(body["path"])
    assert open(body["path"], encoding="utf-8").read() == "第一行\n第二行\n"


def test_uploaded_file_is_reachable_by_agent_tools(client):
    """附件落在项目内，Agent 的 read_file / edit_file 必须能直接操作它。"""
    sid = client.post("/sessions", json={}).get_json()["id"]
    body = _upload(client, b"def f():\n    return 1\n", "sample.py", sid).get_json()
    assert paths.is_allowed_path(body["path"]), "Agent 的工具白名单必须放行附件"
    assert paths.is_path_permitted(body["path"]), "Web 白名单也必须放行（否则无法预览/下载）"


def test_upload_rejects_binary(client):
    sid = client.post("/sessions", json={}).get_json()["id"]
    r = _upload(client, b"\x00\x01\x02\xff\xfe", "blob.bin", sid)
    assert r.status_code == 400
    assert "UTF-8" in r.get_json()["error"]


def test_upload_rejects_non_utf8(client):
    sid = client.post("/sessions", json={}).get_json()["id"]
    r = _upload(client, "中文".encode("gbk"), "gbk.txt", sid)
    assert r.status_code == 400


def test_upload_rejects_oversized(client):
    r = _upload(client, b"x" * (app_module.MAX_UPLOAD_BYTES + 1), "big.txt")
    assert r.status_code == 400
    assert "太大" in r.get_json()["error"]


def test_upload_without_file(client):
    assert client.post("/api/attach", data={}, content_type="multipart/form-data").status_code == 400


def test_upload_sanitises_filename(client):
    """文件名里的路径分隔符 / 危险字符不能穿透到落盘路径。"""
    sid = client.post("/sessions", json={}).get_json()["id"]
    body = _upload(client, b"hi", "../../etc/evil.txt", sid).get_json()
    assert ".." not in body["path"]
    assert body["path"].startswith(paths.UPLOAD_DIR)


def test_upload_requires_auth(anon_client):
    r = anon_client.post(
        "/api/attach",
        data={"file": (io.BytesIO(b"x"), "a.txt")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 401


# ============ 附件进消息 ============


def test_load_attachments_reads_content(client):
    sid = client.post("/sessions", json={}).get_json()["id"]
    up = _upload(client, "正文内容".encode(), "a.txt", sid).get_json()
    loaded = app_module._load_attachments([{"path": up["path"], "name": "a.txt"}])
    assert len(loaded) == 1
    assert loaded[0]["text"] == "正文内容"


def test_load_attachments_ignores_disallowed_or_missing():
    assert app_module._load_attachments([{"path": "/etc/passwd"}]) == []
    assert app_module._load_attachments([{"path": "/不存在/xx.txt"}]) == []
    assert app_module._load_attachments([None, "字符串", {}]) == []


def test_format_attachments_inlines_preview_and_path():
    text = "行\n" * 5000          # 超过预览上限
    block = agent._format_attachments([{"name": "a.md", "path": "/tmp/a.md", "text": text}])
    assert "a.md" in block and "/tmp/a.md" in block
    assert "只显示了前" in block, "超长附件要说明已截断，并告诉模型去 read_file"
    assert len(block) < len(text), "不能把整篇塞进消息"


def test_format_attachments_empty():
    assert agent._format_attachments([]) == ""
    assert agent._format_attachments(None) == ""


# ============ 下载 ============


def test_download_serves_file_as_attachment(client):
    sid = client.post("/sessions", json={}).get_json()["id"]
    up = _upload(client, b"download me", "report.txt", sid).get_json()
    r = client.get(f"/api/download?path={up['path']}")
    assert r.status_code == 200
    assert b"download me" in r.data
    assert "attachment" in r.headers.get("Content-Disposition", "")


def test_download_uses_custom_name(client):
    """附件落盘名带时间戳前缀，下载时应显示原始文件名。"""
    sid = client.post("/sessions", json={}).get_json()["id"]
    up = _upload(client, b"x", "我的报告.md", sid).get_json()
    r = client.get(f"/api/download?path={up['path']}&name=我的报告.md")
    assert "attachment" in r.headers["Content-Disposition"]
    assert "%E6%88%91" in r.headers["Content-Disposition"] or "我的报告" in r.headers["Content-Disposition"]


def test_download_refuses_sensitive_path(client):
    assert client.get("/api/download?path=/etc/passwd").status_code == 403


def test_download_missing_file(client):
    # 路径要在白名单内，否则会先撞 403（那是另一条断言，见下）
    missing = os.path.join(paths.UPLOAD_DIR, "definitely-not-here.txt")
    r = client.get(f"/api/download?path={missing}")
    assert r.status_code == 400


def test_download_requires_path(client):
    assert client.get("/api/download").status_code == 400


def test_download_requires_auth(anon_client):
    assert anon_client.get("/api/download?path=/etc/hosts").status_code == 401
