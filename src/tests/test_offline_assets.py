"""离线可用性回归测试：前端依赖必须全部本地化。

背景：项目宣称「可在本机私有部署」，但早期前端从 CDN 加载 marked /
DOMPurify / highlight.js，断网或内网环境下 Markdown 渲染、代码高亮与
XSS 过滤会全部失效。这里把「不允许再引用外部域名」固化成用例。
"""

import os
import re

import pytest

import app as app_module
import paths

# 前端资源现在统一在 <root>/web/ 下，直接引用 paths 常量避免再次重构时失效
VENDOR_DIR = os.path.join(paths.STATIC_DIR, "vendor")
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


def _frontend_files():
    """前端已拆成模板 + partial + CSS + JS 多文件，外链检查必须覆盖全部。

    只查 index.html 会漏掉 partials 和拆出去的 css/js —— 那正是最容易被
    顺手贴一个 CDN 链接进来的地方。
    """
    roots = [paths.TEMPLATES_DIR, os.path.join(paths.STATIC_DIR, "css"),
             os.path.join(paths.STATIC_DIR, "js")]
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _, names in os.walk(root):
            for n in sorted(names):
                if n.endswith((".html", ".css", ".js")):
                    yield os.path.join(dirpath, n)


def test_no_frontend_file_references_remote_assets():
    remote = []
    for path in _frontend_files():
        text = open(path, encoding="utf-8").read()
        remote += re.findall(
            r'<(?:script|link)\b[^>]*?(?:src|href)\s*=\s*["\'](https?://[^"\']+)',
            text,
            re.I,
        )
        remote += re.findall(r'@import\s+["\'](https?://[^"\']+)', text)
    assert not remote, f"前端仍引用外部资源：{remote}"


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
