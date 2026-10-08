"""前端内联脚本的静态健全性检查。

**为什么要测这个**：前端是单文件 HTML 里的一大段内联 JS，没有构建步骤、没有类型检查。
在里面调用一个不存在的函数（比如重构时删了函数、忘了删调用点），
浏览器只会抛一个 ReferenceError —— 如果恰好发生在 DOMContentLoaded 处理器里，
**同一个 handler 后面的语句就全都不执行了**，而且页面看起来"基本正常"。

真实案例：`loadProjectRoot()` 在改成「工作区」模型时被删掉，但调用点留了下来，
于是紧随其后的 `restoreProjectTreeState()` 从来没跑过 —— 项目树的折叠状态
保存了却永远恢复不了，很久都没人发现。

这里做两件事：
1. 语法检查（靠 node --check，没有 node 就跳过）
2. 找出「调用了但没定义」的函数名
"""

import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, "src")

INDEX = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "web", "templates", "index.html")


def _inline_js() -> str:
    with open(INDEX, encoding="utf-8") as f:
        html = f.read()
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    return "\n".join(blocks).replace("{{ auth_token | tojson }}", '"T"')


# 浏览器 / 语言内置，不算「未定义」
BUILTINS = {
    "if", "for", "while", "switch", "catch", "return", "typeof", "function", "new",
    "await", "else", "do", "delete", "void", "in", "of", "case", "throw", "yield",
    "super", "this", "class", "extends", "instanceof", "try", "finally", "break",
    "continue", "const", "let", "var", "async", "import", "export", "default",
    # 全局对象 / 函数
    "alert", "confirm", "prompt", "fetch", "setTimeout", "setInterval", "clearTimeout",
    "clearInterval", "requestAnimationFrame", "parseInt", "parseFloat", "isNaN",
    "encodeURIComponent", "decodeURIComponent", "encodeURI", "decodeURI", "escape",
    "unescape", "Number", "String", "Boolean", "Array", "Object", "Map", "Set", "WeakMap",
    "Promise", "JSON", "Math", "Date", "RegExp", "Error", "TypeError", "Symbol", "BigInt",
    "localStorage", "sessionStorage", "document", "window", "navigator", "location",
    "console", "URL", "Blob", "File", "FileReader", "FormData", "Headers", "Request",
    "Response", "AbortController", "TextDecoder", "TextEncoder", "Event", "CustomEvent",
    "MutationObserver", "IntersectionObserver", "ResizeObserver", "Image", "Audio",
    "DOMPurify", "marked", "hljs", "AgentCodeCM", "getComputedStyle", "structuredClone",
    "btoa", "atob", "queueMicrotask", "Intl", "Proxy", "Reflect", "Float32Array",
    "Uint8Array", "DataView", "ArrayBuffer", "OffscreenCanvas", "Notification",
    "matchMedia", "open", "close", "focus", "print", "stop", "history", "screen",
    "DataTransfer", "ClipboardEvent", "KeyboardEvent", "MouseEvent", "DragEvent",
    "SpeechSynthesisUtterance", "webkitSpeechRecognition", "AudioContext", "crypto",
    "performance", "Element", "Node", "NodeList", "HTMLElement", "CSS", "Range",
    "requestIdleCallback", "cancelAnimationFrame",
}

# 这些是在别的 script 标签或内联事件里定义的（正则扫不到，白名单兜底）
KNOWN_DYNAMIC = set()


def _defined_names(js: str) -> set:
    names = set()
    # function foo(...)
    names.update(re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)", js))
    # const/let/var foo = ...
    names.update(re.findall(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", js))
    # class Foo
    names.update(re.findall(r"\bclass\s+([A-Za-z_$][\w$]*)", js))
    # 对象字面量里的方法简写：`foo(a, b) {` —— **必须带 { **。
    # 之前漏了这个 {，导致行首的任何调用（`foo();`）都被当成「有定义」，
    # 检查形同虚设。
    names.update(re.findall(r"^\s*([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{", js, re.M))
    # window.foo = / window["foo"] =
    names.update(re.findall(r"window\.([A-Za-z_$][\w$]*)\s*=", js))
    # 函数参数：函数/箭头函数的形参也是本地名字（例如 new Promise((ok, reject) => ...)）
    for params in re.findall(r"\(([^()]*)\)\s*=>", js) + re.findall(r"function\s*\*?\s*\(([^()]*)\)", js):
        for part in params.split(","):
            part = part.strip().lstrip(".").strip()
            m = re.match(r"^\{?\s*([A-Za-z_$][\w$]*)", part)
            if m:
                names.add(m.group(1))
    # 解构
    names.update(re.findall(r"\b(?:const|let|var)\s*\{([^}]*)\}", js))
    return names


def _called_names(js: str) -> set:
    """找出所有「像函数一样被调用」的裸标识符（排除 obj.method() 形式）。"""
    called = set()
    for m in re.finditer(r"([.\w$]?)\b([A-Za-z_$][\w$]*)\s*\(", js):
        prev, name = m.group(1), m.group(2)
        if prev == ".":            # 方法调用，不管
            continue
        called.add(name)
    return called


def test_inline_js_parses():
    """语法必须过（等价于 node --check）。"""
    if not shutil.which("node"):
        pytest.skip("没有 node，跳过语法检查")
    js = _inline_js()
    path = "/tmp/_agentcode_inline_check.js"
    with open(path, "w", encoding="utf-8") as f:
        f.write(js)
    r = subprocess.run(["node", "--check", path], capture_output=True, text=True)
    os.remove(path)
    assert r.returncode == 0, f"内联 JS 语法错误：\n{r.stderr[:800]}"


def test_no_calls_to_undefined_functions():
    """不能调用不存在的函数。

    这类错误的危害不在于它自己报错，而在于**它会中断所在 handler 后面的语句**，
    而且页面看起来还是正常的 —— 极难发现。
    """
    js = _inline_js()

    # 先去掉字符串与注释，避免把里面的内容误当成代码
    stripped = re.sub(r"//[^\n]*", "", js)
    stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.S)
    stripped = re.sub(r"`(?:\\.|[^`\\])*`", '""', stripped, flags=re.S)
    stripped = re.sub(r"'(?:\\.|[^'\\])*'", '""', stripped)
    stripped = re.sub(r'"(?:\\.|[^"\\])*"', '""', stripped)

    defined = _defined_names(js) | BUILTINS | KNOWN_DYNAMIC
    called = _called_names(stripped)

    # 定义里可能一堆参数名混进来，用宽松匹配兜一层
    missing = sorted(n for n in called if n not in defined and not _loosely_defined(stripped, n))
    assert not missing, (
        "以下函数被调用但找不到定义（很可能是重构后遗留的死调用，"
        "会让所在 handler 后面的代码全部不执行）：\n  " + "\n  ".join(missing)
    )


def _loosely_defined(js: str, name: str) -> bool:
    """宽松判断：只要出现过 `name =` / `name:` / `name(` 的赋值或定义形式就算有。"""
    esc = re.escape(name)
    patterns = [
        rf"\b{esc}\s*=\s*(?:function|\()",     # foo = function / foo = () =>
        rf"\b{esc}\s*:\s*(?:function|\()",     # { foo: function }
        rf"['\"]{esc}['\"]\s*:",               # { "foo": ... }
        rf"^\s*{esc}\s*\([^)]*\)\s*\{{",      # 方法简写（必须在行首，且带 {）
        rf"addEventListener\(\s*['\"]{esc}['\"]",
    ]
    return any(re.search(p, js) for p in patterns)
