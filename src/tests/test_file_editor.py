"""右侧文件面板的读写接口测试。

重点覆盖**会毁数据的几条路径**：
- 非 UTF-8（曾用 errors="ignore" 静默丢字节，读出来再存回去会永久写坏 GBK 文件）
- 二进制
- 超大文件（绝不能返回截断内容——保存会把文件尾部删掉）
- 陈旧覆盖（Agent 刚写过同一文件，用户再保存就会静默盖掉它的成果）
"""

import os
import shutil
import stat
import tempfile

import pytest

import app as app_module
import auth
import trash

HOME = os.path.expanduser("~")


@pytest.fixture
def work(monkeypatch):
    """home 下的临时工作区（白名单放行 home，/tmp 会被拒）。"""
    d = tempfile.mkdtemp(prefix=".fp-test-", dir=HOME)
    monkeypatch.setattr(trash, "TRASH_DIR", os.path.join(d, ".trash"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def c(client):
    return client


def _write(d, name, data, binary=False):
    p = os.path.join(d, name)
    if binary:
        with open(p, "wb") as f:
            f.write(data)
    else:
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(data)
    return p


def _read_bytes(p):
    with open(p, "rb") as f:
        return f.read()


# ============ 读取 ============


def test_read_plain_utf8(work, c):
    p = _write(work, "a.py", "import os\nprint('hi')\n")
    r = c.get(f"/api/file_content?path={p}")
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] and d["editable"]
    assert d["content"] == "import os\nprint('hi')\n"
    assert d["eol"] == "lf"
    assert len(d["hash"]) == 64
    assert d["size"] == 22


def test_read_detects_crlf_and_normalises(work, c):
    p = _write(work, "b.txt", "l1\r\nl2\r\n")
    d = c.get(f"/api/file_content?path={p}").get_json()
    assert d["eol"] == "crlf"
    assert "\r" not in d["content"], "编辑器里应统一为 LF，保存时再还原"


def test_read_rejects_binary(work, c):
    p = _write(work, "c.bin", b"\x00\x01\x02hello", binary=True)
    d = c.get(f"/api/file_content?path={p}").get_json()
    assert d["ok"] and d["editable"] is False
    assert d["reason"] == "binary"
    assert "content" not in d


def test_read_rejects_non_utf8(work, c):
    """GBK 文件必须禁止编辑——这是「读出来→改→存回去」会毁文件的路径。"""
    p = os.path.join(work, "gbk.txt")
    with open(p, "wb") as f:
        f.write("中文内容".encode("gbk"))
    d = c.get(f"/api/file_content?path={p}").get_json()
    assert d["editable"] is False
    assert d["reason"] == "not-utf8"
    assert "content" not in d


def test_read_rejects_too_large_and_returns_no_content(work, c):
    p = _write(work, "big.txt", "x" * (app_module.MAX_EDIT_BYTES + 10))
    d = c.get(f"/api/file_content?path={p}").get_json()
    assert d["editable"] is False
    assert d["reason"] == "too-large"
    # 关键：绝不能返回截断内容，否则一保存就把尾部删了
    assert "content" not in d


def test_read_refuses_sensitive_path(c):
    assert c.get("/api/file_content?path=/etc/passwd").status_code == 403


def test_read_requires_path(c):
    assert c.get("/api/file_content").status_code == 400


def test_read_rejects_directory(work, c):
    r = c.get(f"/api/file_content?path={work}")
    assert r.status_code == 400
    assert "不是文件" in r.get_json()["error"]


# ============ 写入 ============


def test_save_writes_and_backs_up(work, c):
    p = _write(work, "a.py", "old\n")
    meta = c.get(f"/api/file_content?path={p}").get_json()

    r = c.put(
        "/api/file_content",
        json={"path": p, "content": "new\n", "expected_hash": meta["hash"], "eol": "lf"},
    )
    d = r.get_json()
    assert r.status_code == 200 and d["ok"]
    assert _read_bytes(p) == b"new\n"
    assert d["backup"], "保存前必须留一份可恢复的备份"
    assert d["hash"] != meta["hash"]

    entries = trash.list_entries()
    assert len(entries) == 1
    assert entries[0]["original_path"] == p
    # 而且真的能恢复回改动前的内容
    ok, _ = trash.restore(entries[0]["id"])
    assert ok and _read_bytes(p) == b"old\n"


def test_save_refuses_stale_hash(work, c):
    """磁盘内容变了（比如 Agent 刚写过）就必须 409，且一个字节都不能改。"""
    p = _write(work, "a.py", "v1\n")
    meta = c.get(f"/api/file_content?path={p}").get_json()

    # 模拟 Agent 在用户编辑期间改了文件
    _write(work, "a.py", "AGENT WROTE THIS\n")

    r = c.put(
        "/api/file_content",
        json={"path": p, "content": "用户的版本\n", "expected_hash": meta["hash"], "eol": "lf"},
    )
    assert r.status_code == 409
    body = r.get_json()
    assert body["error"] == "stale"
    assert body["current_hash"] and body["current_hash"] != meta["hash"]
    assert _read_bytes(p) == b"AGENT WROTE THIS\n", "冲突时必须原样保留磁盘内容"


def test_save_force_overrides_after_conflict(work, c):
    """不带 expected_hash = 用户确认强制覆盖。"""
    p = _write(work, "a.py", "v1\n")
    _write(work, "a.py", "被别人改了\n")
    r = c.put("/api/file_content", json={"path": p, "content": "我的\n", "eol": "lf"})
    assert r.status_code == 200
    assert _read_bytes(p) == "我的\n".encode()


def test_save_preserves_crlf(work, c):
    p = _write(work, "b.txt", "a\r\nb\r\n")
    meta = c.get(f"/api/file_content?path={p}").get_json()
    # 原样存回：字节必须完全一致（否则每次保存都会「整文件都改了」）
    c.put(
        "/api/file_content",
        json={"path": p, "content": meta["content"], "expected_hash": meta["hash"], "eol": "crlf"},
    )
    assert _read_bytes(p) == b"a\r\nb\r\n"


def test_save_preserves_trailing_newline_state(work, c):
    """没有末尾换行的文件，存回去也不能被加上换行。"""
    p = _write(work, "t.txt", "no trailing newline")
    meta = c.get(f"/api/file_content?path={p}").get_json()
    c.put(
        "/api/file_content",
        json={"path": p, "content": meta["content"], "expected_hash": meta["hash"], "eol": "lf"},
    )
    assert _read_bytes(p) == b"no trailing newline"


def test_save_keeps_file_mode(work, c):
    p = _write(work, "run.sh", "#!/bin/sh\necho hi\n")
    os.chmod(p, 0o755)
    meta = c.get(f"/api/file_content?path={p}").get_json()
    c.put(
        "/api/file_content",
        json={"path": p, "content": "#!/bin/sh\necho bye\n", "expected_hash": meta["hash"], "eol": "lf"},
    )
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o755, "原子写不能把可执行位丢掉"


def test_save_refuses_sensitive_path(c, work, monkeypatch):
    r = c.put("/api/file_content", json={"path": "/etc/passwd", "content": "x"})
    assert r.status_code == 403


def test_save_requires_content(work, c):
    p = _write(work, "a.txt", "x")
    assert c.put("/api/file_content", json={"path": p}).status_code == 400


def test_save_rejects_missing_file(work, c):
    r = c.put("/api/file_content", json={"path": os.path.join(work, "nope.txt"), "content": "x"})
    assert r.status_code == 400


def test_save_rejects_oversized_content(work, c):
    p = _write(work, "a.txt", "x")
    big = "y" * (app_module.MAX_EDIT_BYTES + 1)
    r = c.put("/api/file_content", json={"path": p, "content": big})
    assert r.status_code == 400


def test_save_without_body_is_not_415(work, c):
    """回归：request.json 在无 body 时会 415，接口会整个挂掉。"""
    assert c.put("/api/file_content").status_code == 400


def test_file_api_requires_auth(work):
    p = _write(work, "a.txt", "secret")
    anon = app_module.app.test_client()
    assert anon.get(f"/api/file_content?path={p}").status_code == 401
    assert anon.put("/api/file_content", json={"path": p, "content": "x"}).status_code == 401


def test_codemirror_bundle_is_vendored_and_offline(client):
    """前端依赖必须本地可用（项目承诺完全离线）。"""
    import paths

    f = os.path.join(paths.STATIC_DIR, "vendor", "codemirror.min.js")
    assert os.path.isfile(f), "缺少本地化的 CodeMirror"
    assert os.path.getsize(f) > 100_000
    assert client.get("/static/vendor/codemirror.min.js").status_code == 200


def _unused_auth_reference():
    """保留 auth 导入的引用，避免 lint 报未使用（测试客户端由 conftest 注入令牌）。"""
    return auth.get_token()
