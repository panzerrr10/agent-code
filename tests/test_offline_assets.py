"""离线可用性回归测试：前端依赖必须全部本地化。

背景：项目宣称「可在本机私有部署」，但早期前端从 CDN 加载 marked /
DOMPurify / highlight.js，断网或内网环境下 Markdown 渲染、代码高亮与
XSS 过滤会全部失效。这里把「不允许再引用外部域名」固化成用例。
"""

import os
import re

import pytest

import app as app_module

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR_DIR = os.path.join(PROJECT_ROOT, "static", "vendor")
REQUIRED = [
    "marked.min.js",
    "purify.min.js",
    "highlight.min.js",
    "github-dark.min.css",
]


def test_vendor_files_exist_and_non_trivial():
    for name in REQUIRED:
        path = os.path.join(VENDOR_DIR, name)
        assert os.path.isfile(path), f"缺少本地前端依赖：{name}"
        assert os.path.getsize(path) > 500, f"{name} 体积异常，可能是下载失败的空文件"


def test_template_has_no_remote_script_or_link():
    html = open(
        os.path.join(PROJECT_ROOT, "templates", "index.html"), encoding="utf-8"
    ).read()
    remote = re.findall(
        r'<(?:script|link)\b[^>]*?(?:src|href)\s*=\s*["\'](https?://[^"\']+)',
        html,
        re.I,
    )
    assert not remote, f"模板仍引用外部资源：{remote}"


def test_rendered_page_uses_local_assets_only():
    client = app_module.app.test_client()
    body = client.get("/").get_data(as_text=True)
    for name in REQUIRED:
        assert f"/static/vendor/{name}" in body, f"渲染后的页面未引用本地 {name}"
    assert "cdn." not in body, "渲染后的页面仍包含 CDN 引用"


@pytest.mark.parametrize("name", REQUIRED)
def test_static_assets_are_served(name):
    client = app_module.app.test_client()
    resp = client.get(f"/static/vendor/{name}")
    assert resp.status_code == 200, f"{name} 无法通过 /static/vendor 访问"
    assert len(resp.get_data()) > 500
