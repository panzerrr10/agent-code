"""「选择文件夹」对话框的回归测试。

历史问题：原先用 tkinter 的 ``askdirectory``，弹出的是 Tk 自带的老式对话框，
和 Ubuntu 24 其它程序（浏览器上传文件等）的观感完全不一致。
现在优先走 zenity（Ubuntu 24 上是 4.x = GTK4 实现，原生观感），tkinter 仅作兜底。

这些用例不打真实的图形对话框，只验证「选了哪条路径、传了什么参数、
取消时返回什么」——真机弹窗行为另有端到端验证。
"""

import subprocess
import sys

import pytest

sys.path.insert(0, "src")

import app as app_module  # noqa: E402


class _Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def zenity_available(monkeypatch):
    """假装系统里有 zenity。"""
    import shutil

    real = shutil.which
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/zenity" if name == "zenity" else real(name)
    )


def test_pick_folder_prefers_zenity(monkeypatch, zenity_available, tmp_path):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _Result(0, f"{tmp_path}\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    got = app_module._pick_folder("选择工作区文件夹", str(tmp_path))
    assert got == str(tmp_path)

    cmd = captured["cmd"]
    assert cmd[0] == "zenity"
    assert "--file-selection" in cmd
    # 必须是「选目录」而不是「选文件」
    assert "--directory" in cmd
    assert "--title=选择工作区文件夹" in cmd
    # 初始目录要以 / 结尾，zenity 才会直接定位进去
    assert f"--filename={tmp_path}/" in cmd


def test_pick_folder_returns_none_on_cancel(monkeypatch, zenity_available):
    """用户点取消：zenity 退出码 1、无输出 —— 必须返回 None 而不是报错。"""
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _Result(1, ""))
    assert app_module._pick_folder("t", None) is None


def test_pick_folder_ignores_bad_initial_dir(monkeypatch, zenity_available):
    """初始目录不存在时不要传 --filename，否则 zenity 可能定位失败。"""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _Result(0, "/tmp\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    app_module._pick_folder("t", "/不存在的目录/xyz")
    assert not any(c.startswith("--filename=") for c in captured["cmd"])


def test_pick_folder_survives_zenity_timeout(monkeypatch, zenity_available):
    def boom(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(subprocess, "run", boom)
    assert app_module._pick_folder("t", None) is None


def test_pick_folder_falls_back_to_tkinter(monkeypatch):
    """没有 zenity 时应回退 tkinter，而不是直接失败。"""
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)

    fake_tk = type(sys)("tkinter")
    fake_tk.Tk = lambda: type("R", (), {"withdraw": lambda s: None, "attributes": lambda s, *a: None,
                                        "destroy": lambda s: None})()
    fake_fd = type(sys)("tkinter.filedialog")
    fake_fd.askdirectory = lambda **kw: "/tmp/来自tkinter"
    fake_tk.filedialog = fake_fd
    monkeypatch.setitem(sys.modules, "tkinter", fake_tk)
    monkeypatch.setitem(sys.modules, "tkinter.filedialog", fake_fd)

    assert app_module._pick_folder("t", None) == "/tmp/来自tkinter"


# ============ HTTP ============


def test_select_folder_api_ok(client, monkeypatch):
    monkeypatch.setattr(app_module, "_pick_folder", lambda title, initial: "/home/pz/PycharmProjects")
    r = client.post("/api/select_folder", json={"title": "选择工作区文件夹"})
    body = r.get_json()
    assert body["ok"] is True
    assert body["path"] == "/home/pz/PycharmProjects"


def test_select_folder_api_cancel_is_not_an_error(client, monkeypatch):
    """取消要能被前端识别为 cancelled，否则会弹出「打开文件夹选择器失败」。"""
    monkeypatch.setattr(app_module, "_pick_folder", lambda title, initial: None)
    r = client.post("/api/select_folder", json={})
    assert r.status_code == 200
    assert r.get_json() == {"ok": False, "cancelled": True}


def test_select_folder_api_without_body(client, monkeypatch):
    """无 body 也不能 415（request.json 的老坑）。"""
    monkeypatch.setattr(app_module, "_pick_folder", lambda title, initial: None)
    assert client.post("/api/select_folder").status_code == 200
