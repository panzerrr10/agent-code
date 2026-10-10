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


def _called_from_html_attrs(html: str) -> set:
    """找出 HTML 内联属性里调用的函数：``onclick="foo(1)"`` 之类。

    **这个检查是补上一个真实漏洞的**：上面的 ``_called_names`` 只扫 JS 源码，
    而界面上大量交互是写在 HTML 属性里的（``onclick="setPermission(3)"``）。
    结果我删掉一个函数定义时，JS 里的调用点已经一并删了、检查通过，
    **但 HTML 里还指着它** —— 用户一点那个按钮就报错。
    当时漏掉的正是权限等级按钮和附件/图片那一串函数。
    """
    called = set()
    for m in re.finditer(r"""\bon[a-z]+\s*=\s*(?:"([^"]*)"|'([^']*)')""", html):
        body = m.group(1) or m.group(2) or ""
        # 排除 obj.method() —— 只关心裸标识符调用
        for c in re.finditer(r"([.\w$]?)\b([A-Za-z_$][\w$]*)\s*\(", body):
            if c.group(1) == ".":
                continue
            called.add(c.group(2))
    return called


def test_html_inline_handlers_reference_real_functions():
    """HTML 内联 onclick/onchange 里调用的函数必须真的存在。"""
    with open(INDEX, encoding="utf-8") as f:
        html = f.read()
    js = _inline_js()

    called = _called_from_html_attrs(html)
    defined = _defined_names(js) | BUILTINS | KNOWN_DYNAMIC
    missing = sorted(
        n for n in called
        if n not in defined and not _loosely_defined(js, n)
    )
    assert not missing, (
        "这些函数被 HTML 内联属性调用，但没有定义（点那个按钮就会报错）：\n  "
        + "\n  ".join(missing)
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


# ============ SSE 事件处理只有一份 ============

# 前端曾经有**两套** SSE 消费循环：runTask（首次运行）和 resumeRun（续跑）。
# 复制带来的代价不是「代码多」，而是**只往其中一份加处理器，另一份就静默漏掉事件**。
#
# 真实事故：上下文压缩指示（context_stats）、项目约定提示（project_context）、
# 权限拒绝卡片（permission_denied）、轮次事件（run_started）**全都只加在了
# resumeRun 那一份** —— 主路径下这些 UI 一个都不显示，而且单元测试和当时的
# CDP 验证都没发现（CDP 是用合成事件直接调渲染函数，绕过了真实的事件分发）。
#
# 现在两边共用 handleSseEvent / consumeSseStream，这组测试守住这个结构。
CORE_EVENTS = [
    "run_started",        # 第 N 轮
    "text",
    "tool_call",
    "tool_result",
    "token_usage",
    "context_stats",      # 上下文压缩指示
    "project_context",    # 项目约定提示
    "permission_denied",  # 等级 1 拒绝
    "plan_update",
    "confirm_write",
    "confirm_delete",
    "confirm_command",
    "cancelled",
    "error",
    "done",
]

# 多 Agent 专属事件（单 Agent 不会发，但共享处理器也认）
MULTI_AGENT_EVENTS = ["phase", "plan_from_planner", "review_result"]


def _function_body(js: str, signature: str) -> str:
    """取出一个顶层函数的完整函数体（按缩进找配对的结束大括号）。"""
    start = js.index(signature)
    i = js.index("{", start)
    depth = 0
    for j in range(i, len(js)):
        if js[j] == "{":
            depth += 1
        elif js[j] == "}":
            depth -= 1
            if depth == 0:
                return js[start:j + 1]
    raise AssertionError(f"{signature} 的括号不配对")


def test_only_one_sse_consumer_exists():
    """**SSE 只能有一个消费点。**

    这条是这次合并的核心保障：如果将来又有人复制出第二份，
    这个测试会失败，提醒他「加事件处理要改共享函数，不要复制」。
    """
    js = _inline_js()
    readers = js.count("res.body.getReader()")
    assert readers == 1, (
        f"发现 {readers} 处 SSE 消费点（应当只有 consumeSseStream 里那 1 处）。"
        "又复制了一份循环？请改 handleSseEvent，不要复制。"
    )


def test_both_run_paths_use_the_shared_consumer():
    """首次运行与续跑必须都走同一个消费函数。

    注意盯的是 ``_runTaskInner`` 而不是 ``runTask``：
    ``runTask`` 现在只是一层**防重入包装**（`taskStarting` 标志），
    真正干活的在 ``_runTaskInner`` 里。
    """
    js = _inline_js()
    for sig in ("async function _runTaskInner(", "async function resumeRun("):
        body = _function_body(js, sig)
        assert "consumeSseStream(" in body, f"{sig} 没有使用共享的 SSE 消费者"
        assert "handleSseEvent(" not in body, f"{sig} 里又出现了自己的事件分发"


def test_task_entry_is_guarded_against_reentry():
    """``runTask`` 必须有防重入。

    真实事故：``#task`` 上挂了**两个**一模一样的 Enter 监听，都会调 runTask。
    当时没有防重入，只是靠「输入框在同步阶段被清空」让第二次调用提前返回 ——
    **巧合不是设计**：currentSessionId 为空时控制流会在 `await createNewSession()`
    挂起，输入框尚未清空，第二个调用于是又跑一遍，实测发出 2 次 /run、建 2 个会话。
    """
    js = _inline_js()
    # 监听只能有一处
    assert js.count('addEventListener("keydown"') >= 1
    assert js.count('getElementById("task").addEventListener("keydown"') == 0, \
        "#task 上又出现了第二处 keydown 监听"
    # 防重入标志要在真正干活之前检查
    body = _function_body(js, "async function runTask(")
    assert "taskStarting" in body, "runTask 缺少防重入标志"
    assert "if (taskStarting) return;" in body, "runTask 没有在入口处拦截重入"


def test_shared_handler_covers_all_core_events():
    """共享处理器必须覆盖全部核心事件。

    之前就是「事件分支散在两份循环里」，只补了一份 —— 这个测试直接盯住覆盖面。
    """
    js = _inline_js()
    handler = _function_body(js, "function handleSseEvent(")
    missing = [e for e in CORE_EVENTS if f'ev.type === "{e}"' not in handler]
    assert not missing, f"共享处理器漏了这些核心事件：{missing}"
    missing_ma = [e for e in MULTI_AGENT_EVENTS if f'ev.type === "{e}"' not in handler]
    assert not missing_ma, f"共享处理器漏了这些多 Agent 事件：{missing_ma}"


def test_sse_errors_are_not_silently_swallowed():
    """SSE 事件处理的异常**必须打日志**。

    原来是空的 `catch (e) { }`。后果：resumeRun 里
    `finishRoundCard(ctx, mySessionId)` 的 ReferenceError（那个函数里根本没有
    mySessionId）被静默吞掉，表现为「续跑后轮次卡片不出现」——查起来全靠猜。
    """
    js = _inline_js()
    consumer = _function_body(js, "async function consumeSseStream(")
    assert "catch" in consumer, "consumeSseStream 应当捕获事件处理异常"
    assert "console.error" in consumer or "console.warn" in consumer, \
        "捕获了却什么都不打 —— 这正是上次藏住 ReferenceError 的原因"


# ============ 变量声明 ============

JS_KEYWORDS = {
    "if", "for", "while", "return", "typeof", "new", "var", "let", "const", "case",
    "delete", "void", "in", "of", "do", "else", "try", "finally", "throw", "yield",
    "await", "switch", "catch", "default", "instanceof", "function", "class",
}


def _declared_vars(js: str) -> set:
    """所有被声明过的标识符：let/const/var（含同行多声明与解构）、函数、参数、catch、for-of。"""
    declared = set()
    for m in re.finditer(r"\b(?:let|const|var)\s+([^;{}\n]*)", js):
        for part in m.group(1).split(","):
            name = part.split("=")[0].strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", name):
                declared.add(name)
            declared.update(re.findall(r"[{[]\s*([A-Za-z_$][\w$]*)", part))
    declared |= set(re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)", js))
    declared |= set(re.findall(r"\bclass\s+([A-Za-z_$][\w$]*)", js))
    for m in re.finditer(r"\bfunction\s*[A-Za-z_$\w]*\s*\(([^)]*)\)", js):
        for x in m.group(1).split(","):
            n = x.strip().split("=")[0].strip().lstrip(".")
            if re.fullmatch(r"[A-Za-z_$][\w$]*", n):
                declared.add(n)
    for m in re.finditer(r"\(([^()]*)\)\s*=>", js):
        for x in m.group(1).split(","):
            n = x.strip().split("=")[0].strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", n):
                declared.add(n)
        declared.update(re.findall(r"[{[]\s*([A-Za-z_$][\w$]*)", m.group(1)))
    for m in re.finditer(r"([A-Za-z_$][\w$]*)\s*=>", js):
        declared.add(m.group(1))
    declared |= set(re.findall(r"\bcatch\s*\(\s*([A-Za-z_$][\w$]*)", js))
    declared |= set(re.findall(r"\bfor\s*\(\s*(?:let|const|var)\s+([A-Za-z_$][\w$]*)", js))
    return declared


def test_no_assignment_to_undeclared_variable():
    """给一个没声明过的变量赋值 = 制造了一个隐式全局。

    松散模式下浏览器不报错，但**它是真实的定时炸弹**：
    - 从一个作用域泄漏到全局，和别的代码串味
    - 一旦哪天上 strict mode / 打包，立刻 ReferenceError

    真实事故：一次大范围重构中，`let pendingAttachments = []` 和
    `let currentProviderId = ""` 这四行声明被连带删掉了，而引用还在。
    函数级的「调用了未定义的函数」检查**完全看不到变量**，
    是浏览器里跑真实任务才炸出 `pendingAttachments is not defined` 的。
    """
    js = _inline_js()
    stripped = re.sub(r"//[^\n]*", "", js)
    stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.S)
    stripped = re.sub(r"`(?:\\.|[^`\\])*`", '""', stripped, flags=re.S)
    stripped = re.sub(r"'(?:\\.|[^'\\])*'", '""', stripped)
    stripped = re.sub(r'"(?:\\.|[^"\\])*"', '""', stripped)

    assigned = set()
    for m in re.finditer(r"(^|[;{}\n])\s*([A-Za-z_$][\w$]*)\s*=(?!=)", stripped):
        assigned.add(m.group(2))
    assigned -= JS_KEYWORDS

    declared = _declared_vars(stripped) | JS_KEYWORDS | BUILTINS
    missing = sorted(n for n in assigned if n not in declared)
    assert not missing, (
        "这些变量被赋值但从未声明（会变成隐式全局，且 strict mode 下直接报错）：\n  "
        + "\n  ".join(missing)
    )


def test_css_braces_are_balanced():
    """CSS 大括号必须配对。

    样式块写坏（少一个 `}`）不会让页面报错，而是**后面所有规则静默失效** ——
    表现成「改了样式但没生效」，极难定位。这次追加了一整块设计尺度，
    值得加一条兜底。
    """
    with open(INDEX, encoding="utf-8") as f:
        html = f.read()
    blocks = re.findall(r"<style[^>]*>(.*?)</style>", html, re.S)
    assert blocks, "没找到 <style> 块"
    for i, css in enumerate(blocks):
        # 去掉注释再数，注释里的 { } 不算
        stripped = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        assert stripped.count("{") == stripped.count("}"), (
            f"第 {i+1} 个 <style> 块括号不配对："
            f"{{ × {stripped.count('{')}，}} × {stripped.count('}')}"
        )


def test_design_tokens_are_defined():
    """设计尺度必须存在 —— 它是「统一按钮/间距/圆角」的唯一来源。

    如果哪天有人把它删了，样式不会报错，但按钮会悄悄退回各写各的状态。
    """
    with open(INDEX, encoding="utf-8") as f:
        html = f.read()
    for token in ("--sp-1", "--r-md", "--fs-md", "--accent", "--shadow-1", "--bg-header"):
        assert f"{token}:" in html, f"设计尺度里缺少 {token}"
    assert "body.dark {" in html, "缺少暗色模式的 token 覆盖"
