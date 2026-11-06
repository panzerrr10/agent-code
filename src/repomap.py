"""多语言符号提取 + 仓库索引（repo map）。

为什么单独抽这一层：

1. 旧的 ``tools.list_symbols`` 只认 Python，而且用 ``isinstance(node, ast.FunctionDef)``
   判断函数——``ast.AsyncFunctionDef`` **不是** ``ast.FunctionDef`` 的子类，于是所有
   ``async def`` 都看不见。异步函数现在到处都是，这个漏检很致命，所以本模块一律用
   ``(ast.FunctionDef, ast.AsyncFunctionDef)`` 元组判断。
2. Agent 每轮都靠逐个 ``read_file`` 认识代码库太贵。先给 LLM 一份紧凑索引（有哪些文件、
   每个文件有哪些符号、哪个文件被别的文件引用得多），它能少读一大半文件。
3. 判断"这个文件该不该按文本搜索"需要先看扩展名和内容，这个判断散落在各处很容易不一致，
   统一放在这里。

设计取舍（都写在对应函数上方，这里先给结论）：

* **零项目内依赖**：只用标准库，方便单独测试、也方便在沙箱里直接跑。
* **Python 走 AST（精确），其它语言走正则（轻量启发式）**。正则只求"稳、不炸、不漏大类"：
  宁可少报几个边缘符号，也不要因为误匹配（比如把 ``if (x) {`` 当成函数、把 ``return foo(1)``
  当定义）让 LLM 产生幻觉。
* **end_line 尽量求，求不到就退化成起始行**：花括号语言做括号配对，Ruby/ObjC 做
  关键字（``end``/``@end``）配对，配对失败就只报一行，绝不猜测。

对外接口（签名固定，上层按这个调用）：``EXT_LANGUAGE`` / ``TEXT_EXTENSIONS`` /
``BINARY_EXTENSIONS`` / ``Symbol`` / ``language_of`` / ``is_probably_text`` /
``extract_symbols`` / ``list_symbols_text`` / ``find_definition`` / ``build_repo_map`` /
``detect_language_summary``。
"""

import ast
import os
import re
from dataclasses import dataclass, replace

# ============ 扩展名 / 语言 ============

# 扩展名（小写、含点）-> 语言名。只放能提取符号的语言。
EXT_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".hxx": "cpp",
    ".cs": "csharp",
    ".go": "go",
    ".rs": "rust",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".swift": "swift",
    ".php": "php",
    ".rb": "ruby",
    ".scala": "scala",
    ".m": "objectivec",
}

# 该按文本处理的后缀：所有语言扩展名 + 常见纯文本/配置类。
# 上层 search_files 用这个集合判断"要不要在这个文件里做文本搜索"。
TEXT_EXTENSIONS: set[str] = set(EXT_LANGUAGE) | {
    ".txt",
    ".md",
    ".rst",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".csv",
    ".tsv",
    ".sql",
    ".sh",
    ".bash",
    ".zsh",
    ".bat",
    ".ps1",
    ".xml",
    ".html",
    ".htm",
    ".css",
    ".scss",
    ".less",
    ".vue",
    ".svelte",
    ".gradle",
    ".properties",
    ".dockerfile",
    ".gitignore",
    ".env",
    ".lua",
    ".pl",
    ".r",
    ".dart",
}

# 明确当二进制处理的后缀。注意这里有双段后缀（.min.js / .min.css），
# os.path.splitext 只认最后一段，所以判断时用 endswith 而不是 splitext。
BINARY_EXTENSIONS: set[str] = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".bmp",
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
    ".bz2",
    ".xz",
    ".7z",
    ".jar",
    ".war",
    ".class",
    ".pyc",
    ".pyo",
    ".so",
    ".dll",
    ".dylib",
    ".exe",
    ".bin",
    ".o",
    ".a",
    ".lib",
    ".mp3",
    ".mp4",
    ".avi",
    ".mov",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".eot",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".lock",
    ".min.js",
    ".min.css",
}

# str.endswith 只接受 tuple，这里预排序一次，避免每次调用都重建。
_BINARY_SUFFIXES = tuple(sorted(BINARY_EXTENSIONS))


@dataclass(frozen=True)
class Symbol:
    """一个符号（函数/类/…）在文件里的位置。

    ``line``/``end_line`` 都是 1-based 且闭区间；拿不到结束位置时 ``end_line == line``，
    这样上层做"读这个定义"时至少能读到起始行而不会崩。
    """

    kind: str
    name: str
    line: int
    end_line: int
    container: str
    detail: str


# ============ 基础判断 ============


def language_of(path: str) -> str | None:
    """按扩展名返回语言名，未知返回 None。大小写不敏感。"""
    if not path:
        return None
    return EXT_LANGUAGE.get(os.path.splitext(path)[1].lower())


def is_probably_text(path: str, sample_bytes: int = 8192) -> bool:
    """判断文件是否该按文本处理。

    顺序很重要：先看后缀（二进制后缀直接 False，省一次 IO），再读开头一段找 ``\\x00``。
    任何一步失败都返回 False —— 上层会拿这个结果去决定要不要读文件，
    这里抛异常会把"探测"变成"事故"。
    """
    try:
        name = os.path.basename(path).lower()
        if name.endswith(_BINARY_SUFFIXES):
            return False
        with open(path, "rb") as handle:
            chunk = handle.read(sample_bytes)
    except Exception:
        # 文件不存在 / 是目录 / 没有权限：都当成"不是文本"
        return False
    # 空文件没有 \x00，按文本处理
    return b"\x00" not in chunk


def _read_text(path: str) -> str | None:
    """读整个文件为文本；读失败或疑似二进制返回 None。"""
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except (OSError, ValueError):
        return None
    if b"\x00" in raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        # 老代码库里有非 UTF-8 源文件；替换错误字符也比整份索引少一个文件强
        return raw.decode("utf-8", errors="replace")


# ============ 行号工具 ============


def _line_offsets(text: str) -> list[int]:
    """每行起始偏移（按 \\n 切）。用它把匹配偏移换算成行号。"""
    offsets = [0]
    for index, char in enumerate(text):
        if char == "\n":
            offsets.append(index + 1)
    return offsets


def _line_of(offsets: list[int], position: int) -> int:
    """偏移 -> 1-based 行号（二分；符号多的大文件里线性扫描会变成 O(n²)）。"""
    low = 0
    high = len(offsets) - 1
    while low < high:
        middle = (low + high + 1) // 2
        if offsets[middle] <= position:
            low = middle
        else:
            high = middle - 1
    return low + 1


def _squash(value: str) -> str:
    """把多行/多余空白压成单空格，索引文本才够紧凑。"""
    return re.sub(r"\s+", " ", value).strip()


def _short_name(value: str) -> str:
    """``*pkg.Type`` -> ``Type``；只用于容器名，避免渲染出 ``a.b.a.b`` 这种噪音。"""
    text = value.strip().strip("*&").strip()
    text = text.rsplit("::", 1)[-1]
    return text.rsplit(".", 1)[-1].strip()


# ============ 花括号配对（求 end_line） ============


def _skip_quoted(text: str, start: int, quote: str) -> int:
    """跳过一段字符串，返回结束偏移。未闭合时按行/按文件截断，避免吞掉整个文件。"""
    index = start + 1
    size = len(text)
    while index < size:
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == quote:
            return index + 1
        if char == "\n" and quote != "`":
            return index
        index += 1
    return size


def _skip_char_literal(text: str, start: int) -> int | None:
    """``'a'``/``'\\n'`` 这类字符字面量返回结束偏移。

    Rust 的生命周期写法 ``'a``（``impl<'a> Foo<'a>``）和它长得像，这里靠"闭合引号是否
    紧跟在单个字符/转义序列之后"来区分，区分不了就当生命周期（返回 None），
    否则会把 ``'a> Foo<'`` 之间当成字符串，花括号深度全乱。
    """
    size = len(text)
    if start + 2 >= size:
        return None
    second = text[start + 1]
    if second == "\n":
        return None
    if second == "\\":
        index = start + 2
        while index < size and index - start <= 12:
            char = text[index]
            if char == "'":
                return index + 1
            if char == "\n":
                return None
            index += 1
        return None
    if text[start + 2] == "'":
        return start + 3
    return None


def _scan_brace_block(text: str, start: int) -> int | None:
    """从 start 起找第一个 ``{`` 并做配对，返回配对 ``}`` 之后的偏移；失败返回 None。

    需要注意的坑：

    * 若规则的匹配本身就吃掉了 ``{``（``type X struct {``、``namespace n {``），
      起始位置就落在花括号之后，必须手动补上这一层，否则第一个 ``}`` 会被当成多余的。
    * 若在遇到第一个 ``{`` 之前先遇到顶层 ``;``，说明这是声明（``int foo(void);``）
      而不是定义，必须返回 None，否则会把下一个函数的函数体算成它的结束位置。
    * 若在第一个 ``{`` 之前先出现空行，说明声明已经结束（``class Foo(val x: Int)`` 之后
      隔一行才是下一个函数的 ``{``），同样返回 None。
    * 注释/字符串里的花括号要跳过，``char *s = "}";`` 不然深度就对不上了。
    """
    index = start
    size = len(text)
    depth = 0
    opened = False
    probe = start - 1
    while probe >= 0 and text[probe] in " \t":
        probe -= 1
    if probe >= 0 and text[probe] == "{":
        depth = 1
        opened = True
    while index < size:
        char = text[index]
        if char == "/" and index + 1 < size:
            following = text[index + 1]
            if following == "/":
                newline = text.find("\n", index)
                if newline < 0:
                    return None
                index = newline
                continue
            if following == "*":
                end = text.find("*/", index + 2)
                if end < 0:
                    return None
                index = end + 2
                continue
        if char in ('"', "`"):
            index = _skip_quoted(text, index, char)
            continue
        if char == "'":
            end = _skip_char_literal(text, index)
            if end is not None:
                index = end
                continue
            index += 1
            continue
        if char == "{":
            depth += 1
            opened = True
        elif char == "}":
            depth -= 1
            if opened and depth <= 0:
                return index + 1
            if depth < 0:
                return None
        elif char == ";" and not opened and depth == 0:
            return None
        elif char == "\n" and not opened:
            following = index + 1
            while following < size and text[following] in " \t\r":
                following += 1
            if following < size and text[following] == "\n":
                return None
        index += 1
    return None


def _brace_block_end(text: str, start: int, offsets: list[int]) -> int | None:
    """花括号语言的 end_line（1-based）；配对失败返回 None。"""
    close = _scan_brace_block(text, start)
    if close is None:
        return None
    return _line_of(offsets, close - 1)


# ============ 关键字配对（Ruby / Objective-C 求 end_line） ============

_RUBY_OPEN_RE = re.compile(r"^(?:class|module|def|if|unless|case|begin|while|until|for)\b")
_RUBY_DO_RE = re.compile(r"\bdo\b[ \t]*(?:\|[^|]*\|)?[ \t]*$")
_RUBY_END_RE = re.compile(r"(?<![\w.])end\b")
_OBJC_OPEN_RE = re.compile(r"^@(?:interface|implementation|protocol)\b")
_OBJC_END_RE = re.compile(r"@end\b")


def _keyword_block_end(
    lines: list[str],
    start_line: int,
    open_re: re.Pattern[str],
    close_re: re.Pattern[str],
    extra_open_re: re.Pattern[str] | None = None,
) -> int | None:
    """靠成对关键字（``def…end``、``@implementation…@end``）找结束行。

    Ruby 一行里可能有多个块（``each do |x|``）也可能一个块都不开，所以这里只是启发式：
    数不清就返回 None，让上层退化成"结束行 = 起始行"，绝不猜一个更长的范围出去。
    """
    depth = 1  # 起始行本身算一个已打开的块
    for index in range(start_line - 1, len(lines)):
        stripped = lines[index].strip()
        if not stripped or stripped.startswith("#"):
            continue
        closes = len(close_re.findall(stripped))
        extra = 1 if extra_open_re is not None and extra_open_re.search(stripped) else 0
        if index == start_line - 1:
            # 起始行的开关键字已经算进 depth=1，但同一行上可能还有 end / do
            depth += closes + extra
        else:
            depth += (1 if open_re.match(stripped) else 0) + extra - closes
        if depth <= 0:
            return index + 1
    return None


# ============ Python：AST 精确提取 ============


def _python_args(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """``(a, b, *args, **kwargs)`` 形式的参数串。

    注意 ``a.vararg``/``a.kwarg`` 在 3.12 里仍是旧式字段名，别忘了独立处理，
    否则 ``def f(*args)`` 会渲染成 ``f()``。
    """
    args = node.args
    names = [item.arg for item in getattr(args, "posonlyargs", [])]
    names += [item.arg for item in args.args]
    if args.vararg is not None:
        names.append("*" + args.vararg.arg)
    elif args.kwonlyargs:
        names.append("*")
    names += [item.arg for item in args.kwonlyargs]
    if args.kwarg is not None:
        names.append("**" + args.kwarg.arg)
    return "(" + ", ".join(names) + ")"


def _python_bases(node: ast.ClassDef) -> str:
    """``extends Base, Mixin``；没有基类返回空串。"""
    names: list[str] = []
    for base in node.bases:
        try:
            names.append(_squash(ast.unparse(base)))
        except Exception:
            # ast.unparse 对个别畸形节点会抛异常，基类只是补充信息，丢掉即可
            continue
    if not names:
        return ""
    return "extends " + ", ".join(names)


def _walk_python(body: list[ast.stmt], container: str, parent_kind: str, out: list[Symbol]) -> None:
    """递归收集 Python 符号。

    ``parent_kind`` 用来区分"类里的函数=方法"和"方法里的嵌套函数=普通函数"，
    只看名字或缩进都判断不出来。
    """
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # 必须写成元组：ast.AsyncFunctionDef 不是 ast.FunctionDef 的子类
            kind = "method" if parent_kind == "class" else "function"
            end = getattr(node, "end_lineno", None) or node.lineno
            out.append(Symbol(kind, node.name, node.lineno, end, container, _python_args(node)))
            _walk_python(node.body, node.name, "function", out)
        elif isinstance(node, ast.ClassDef):
            end = getattr(node, "end_lineno", None) or node.lineno
            out.append(Symbol("class", node.name, node.lineno, end, container, _python_bases(node)))
            _walk_python(node.body, node.name, "class", out)


def _python_symbols(path: str) -> list[Symbol]:
    """Python 文件的符号；解析失败/文件不存在/二进制都返回 []。"""
    text = _read_text(path)
    if text is None:
        return []
    try:
        tree = ast.parse(text)
    except Exception:
        # SyntaxError 最常见，但极深嵌套还会抛 RecursionError，一律当"无法解析"
        return []
    out: list[Symbol] = []
    _walk_python(tree.body, "", "module", out)
    return out


# ============ 其它语言：正则启发式 ============


@dataclass(frozen=True)
class _Rule:
    """一条提取规则。

    ``args_group`` 抓到的内容会渲染成 ``(a, b)``，``detail_group`` 直接当 detail 用
    （比如 ``extends Base``）。``kind_if_container`` 用于"同一个关键字在顶层是函数、
    在类/impl 里是方法"的语言（Rust/Go/Kotlin…）。
    """

    kind: str
    pattern: re.Pattern[str]
    args_group: str = ""
    detail_group: str = ""
    container_group: str = ""
    container_from_name: bool = False
    kind_if_container: str = ""
    end_style: str = "brace"


def _rule(
    pattern: str,
    kind: str,
    args_group: str = "",
    detail_group: str = "",
    container_group: str = "",
    container_from_name: bool = False,
    kind_if_container: str = "",
    end_style: str = "brace",
) -> _Rule:
    return _Rule(
        kind=kind,
        pattern=re.compile(pattern),
        args_group=args_group,
        detail_group=detail_group,
        container_group=container_group,
        container_from_name=container_from_name,
        kind_if_container=kind_if_container,
        end_style=end_style,
    )


_HEAD = r"(?m)^[ \t]*"

# 语句关键字：`new Foo()`、`return foo(1)` 这类语句长得就像"类型 名字("，
# 不挡住就会凭空多出一堆函数/方法符号。必须紧跟在行首（或"返回类型"之前）判断。
_STMT_GUARD = (
    r"(?!(?:new|return|if|for|while|switch|catch|assert|throw|else|do|case|instanceof|"
    r"super|this|await|yield|synchronized|using|lock|sizeof|delete|goto|break|continue)\b)"
)

# ---- Java ----
_JAVA_MODS = (
    r"(?:(?:public|protected|private|abstract|final|static|sealed|non-sealed|strictfp|"
    r"transient|volatile|native|synchronized|default)[ \t]+)*"
)
_JAVA_RULES = [
    # record 要在 class 之前：两者互斥，但顺序决定同行的 kind 归属
    _rule(
        _HEAD + _JAVA_MODS + r"record[ \t]+(?P<name>\w+)(?P<detail>\([^)\n]*\)[^{;\n]*)",
        "record",
    ),
    _rule(_HEAD + _JAVA_MODS + r"class[ \t]+(?P<name>\w+)(?P<detail>[^{;\n]*)", "class"),
    _rule(_HEAD + _JAVA_MODS + r"interface[ \t]+(?P<name>\w+)(?P<detail>[^{;\n]*)", "interface"),
    _rule(_HEAD + _JAVA_MODS + r"enum[ \t]+(?P<name>\w+)(?P<detail>[^{;\n]*)", "enum"),
    # 方法：`修饰符 返回类型 名字(`；`if (x) {` 这类因为类型和名字之间必须有空白而天然不匹配
    _rule(
        _HEAD
        + _STMT_GUARD
        + _JAVA_MODS
        + r"(?:[\w$\[\]<>,.?]+)[ \t]+(?P<name>\w+)[ \t]*\((?P<args>[^;{)]*)\)",
        "method",
        args_group="args",
    ),
]

# ---- C / C++（.h/.hpp 共用一套，C++ 的头文件也就能覆盖到） ----
_C_MODS = (
    r"(?:(?:static|inline|extern|virtual|explicit|friend|constexpr|consteval|constinit|"
    r"const|volatile|unsigned|signed|register|auto|mutable|typedef|struct|enum|union|class|"
    r"typename|public|private|protected|override|final)[ \t]+)*"
)
_C_RULES = [
    _rule(_HEAD + r"namespace[ \t]+(?P<name>[\w:]+)[ \t]*\{?", "namespace"),
    _rule(_HEAD + r"(?:typedef[ \t]+)?struct[ \t]+(?P<name>\w+)(?P<detail>[^{;\n]*)", "struct"),
    _rule(_HEAD + r"(?:typedef[ \t]+)?union[ \t]+(?P<name>\w+)(?P<detail>[^{;\n]*)", "struct"),
    _rule(_HEAD + r"(?:typedef[ \t]+)?class[ \t]+(?P<name>\w+)(?P<detail>[^{;\n]*)", "class"),
    _rule(
        _HEAD + r"(?:typedef[ \t]+)?enum(?:[ \t]+class)?[ \t]+(?P<name>\w+)(?P<detail>[^{;\n]*)",
        "enum",
    ),
    # 函数定义：`类型 名字(`。两层保护：
    #   1) 行首关键字断言 + "前缀必须以类型字符开头"——后者很关键：`^[ \t]*` 会回溯，
    #      不锚住的话缩进本身会被吃进前缀里，`    return foo(1);` 就会变成函数 foo。
    #   2) 名字前再挡一次语句关键字（`if (x) {`、`switch (y) {`）。
    _rule(
        _HEAD
        + r"(?!(?:if|for|while|switch|return|else|do|case|sizeof|catch|new|delete|throw|goto|"
        + r"using|typedef|namespace|class|struct|enum|union|template)\b)"
        + r"(?=[\w:*&<>,\[\]])"
        + r"(?:"
        + _C_MODS
        + r"[\w:*&<>,\[\] \t]+?)"
        # 兼容 `int *foo(`、`char * foo(`；`Foo::Foo(` 靠前缀末尾的 `::` 兜住（无空白也行）
        + r"(?:[ \t]+|(?<=[:*&]))"
        + _STMT_GUARD
        + r"[*&]*[ \t]*(?P<name>[A-Za-z_]\w*)[ \t]*\((?P<args>[^;{)]*)\)",
        "function",
        args_group="args",
    ),
]

# ---- Go ----
_GO_RULES = [
    # `func (r *T) Name(` 是方法：接收者类型当 container，kind 升级成 method。
    # 接收者整体用 [\w.]+ 抓再取短名——用 `[\w.]*\.?` 这种写法会发生
    # "Point 被回溯成 Poin + t" 的经典贪婪坑。
    _rule(
        _HEAD
        + r"func[ \t]+(?:\([ \t]*(?:\w+[ \t]+)?\*?[ \t]*(?P<container>[\w.]+)[ \t]*\)[ \t]*)?"
        + r"(?P<name>\w+)[ \t]*(?:\[[^\]\n]*\])?[ \t]*\((?P<args>[^;{)]*)\)",
        "function",
        args_group="args",
        container_group="container",
        kind_if_container="method",
    ),
    _rule(_HEAD + r"type[ \t]+(?P<name>\w+)(?:\[[^\]\n]*\])?[ \t]+struct[ \t]*\{", "struct"),
    _rule(_HEAD + r"type[ \t]+(?P<name>\w+)(?:\[[^\]\n]*\])?[ \t]+interface[ \t]*\{", "interface"),
    # 别名/基础类型定义没有块，必须用 end_style="line"（Go 里没有分号，靠 `;` 兜不住）；
    # 这两条也要排在 struct/interface 之后，靠 (行, 名字) 去重让前者优先
    _rule(
        _HEAD + r"type[ \t]+(?P<name>\w+)(?:\[[^\]\n]*\])?[ \t]*=[ \t]*(?P<detail>[\w\[\]*., \t]+)",
        "type",
        end_style="line",
    ),
    _rule(
        _HEAD + r"type[ \t]+(?P<name>\w+)(?:\[[^\]\n]*\])?[ \t]+(?P<detail>[\w\[\]*., \t]+)",
        "type",
        end_style="line",
    ),
]

# ---- Rust ----
_RUST_MODS = r"(?:(?:pub(?:\([^)\n]*\))?|async|unsafe|extern|const|default)[ \t]+)*"
_RUST_RULES = [
    # 名字后面允许任意非 `(`、非换行内容（泛型/where），保证 `fn f<T: Fn()->u8>(x: T)` 也能命中
    _rule(
        _HEAD + _RUST_MODS + r"fn[ \t]+(?P<name>\w+)[^(\n]*\((?P<args>[^;{)]*)\)",
        "function",
        args_group="args",
        kind_if_container="method",
    ),
    _rule(_HEAD + _RUST_MODS + r"struct[ \t]+(?P<name>\w+)", "struct"),
    _rule(_HEAD + _RUST_MODS + r"enum[ \t]+(?P<name>\w+)", "enum"),
    _rule(_HEAD + _RUST_MODS + r"trait[ \t]+(?P<name>\w+)", "trait"),
    _rule(_HEAD + r"(?:pub(?:\([^)\n]*\))?[ \t]+)?mod[ \t]+(?P<name>\w+)", "namespace"),
    # `impl Display for Foo` 取 for 之后的类型；impl 块内的 fn 靠嵌套归属拿到同一个 container
    _rule(
        _HEAD + r"impl(?:<[^>\n]*>)?[ \t]+(?:[\w:<>, \t]+?[ \t]+for[ \t]+)?(?P<name>[\w:]+)",
        "impl",
        container_from_name=True,
    ),
]

# ---- JavaScript / TypeScript ----
_JS_MODS = r"(?:(?:export|default|declare|abstract|async)[ \t]+)*"
_JS_RULES = [
    _rule(
        _HEAD + _JS_MODS + r"function[ \t]*\*?[ \t]*(?P<name>\w+)[ \t]*\((?P<args>[^;{)]*)\)",
        "function",
        args_group="args",
    ),
    _rule(_HEAD + _JS_MODS + r"class[ \t]+(?P<name>\w+)(?P<detail>[^{;\n]*)", "class"),
    _rule(
        _HEAD
        + _JS_MODS
        + r"(?:const|let|var)[ \t]+(?P<name>\w+)[ \t]*=[ \t]*(?:async[ \t]+)?"
        + r"\((?P<args>[^;{)]*)\)[ \t]*(?::[^=;\n]*)?=>",
        "function",
        args_group="args",
    ),
    _rule(
        _HEAD
        + _JS_MODS
        + r"(?:const|let|var)[ \t]+(?P<name>\w+)[ \t]*=[ \t]*(?:async[ \t]+)?"
        + r"function[ \t]*\*?[ \t]*\w*[ \t]*\((?P<args>[^;{)]*)\)",
        "function",
        args_group="args",
    ),
    # 单参数无括号的箭头函数：必须排在带括号的规则之后
    _rule(
        _HEAD
        + _JS_MODS
        + r"(?:const|let|var)[ \t]+(?P<name>\w+)[ \t]*=[ \t]*(?:async[ \t]+)?"
        + r"(?P<args>[A-Za-z_$][\w$]*)[ \t]*=>",
        "function",
        args_group="args",
    ),
]
_TS_EXTRA = [
    _rule(_HEAD + _JS_MODS + r"interface[ \t]+(?P<name>\w+)(?P<detail>[^{;\n]*)", "interface"),
    _rule(
        _HEAD + _JS_MODS + r"type[ \t]+(?P<name>\w+)(?:<[^>\n]*>)?[ \t]*=[ \t]*",
        "type",
    ),
    _rule(_HEAD + _JS_MODS + r"(?:const[ \t]+)?enum[ \t]+(?P<name>\w+)", "enum"),
    _rule(_HEAD + _JS_MODS + r"namespace[ \t]+(?P<name>[\w.]+)", "namespace"),
]
_TS_RULES = [*_TS_EXTRA, *_JS_RULES]

# ---- C# ----
_CS_MODS = (
    r"(?:(?:public|private|protected|internal|static|virtual|override|abstract|sealed|"
    r"partial|async|readonly|extern|unsafe|new)[ \t]+)*"
)
_CSHARP_RULES = [
    _rule(_HEAD + r"namespace[ \t]+(?P<name>[\w.]+)", "namespace"),
    _rule(_HEAD + _CS_MODS + r"record[ \t]+(?P<name>\w+)(?P<detail>[^{;(\n]*)", "record"),
    _rule(_HEAD + _CS_MODS + r"interface[ \t]+(?P<name>\w+)(?P<detail>[^{;\n]*)", "interface"),
    _rule(_HEAD + _CS_MODS + r"struct[ \t]+(?P<name>\w+)(?P<detail>[^{;\n]*)", "struct"),
    _rule(_HEAD + _CS_MODS + r"enum[ \t]+(?P<name>\w+)(?P<detail>[^{;\n]*)", "enum"),
    _rule(_HEAD + _CS_MODS + r"class[ \t]+(?P<name>\w+)(?P<detail>[^{;\n]*)", "class"),
    # 属性（public int Foo { get; set; }）没有括号，天然不会被当成方法
    _rule(
        _HEAD
        + _STMT_GUARD
        + _CS_MODS
        + r"(?:[\w<>\[\],.?]+)[ \t]+(?P<name>\w+)[ \t]*\((?P<args>[^;{)]*)\)",
        "method",
        args_group="args",
    ),
]

# ---- Kotlin ----
_KT_MODS = (
    r"(?:(?:public|private|protected|internal|open|override|suspend|inline|operator|infix|"
    r"tailrec|external|abstract|final|const|lateinit|vararg|data|sealed|enum|annotation|inner|"
    r"value|companion|expect|actual|crossinline|noinline|reified)[ \t]+)*"
)
_KT_RULES = [
    _rule(
        _HEAD
        + _KT_MODS
        + r"fun[ \t]+(?:<[^>\n]*>[ \t]*)?(?:[\w.<>?, \t]+\.)?(?P<name>\w+)[ \t]*"
        + r"\((?P<args>[^;{)]*)\)",
        "function",
        args_group="args",
        kind_if_container="method",
    ),
    # `enum class` 必须排在 class 之前，否则 `enum` 会先被 _KT_MODS 吃掉、kind 变成 class
    _rule(_HEAD + _KT_MODS + r"enum[ \t]+class[ \t]+(?P<name>\w+)", "enum"),
    _rule(_HEAD + _KT_MODS + r"class[ \t]+(?P<name>\w+)(?P<detail>[^{;\n]*)", "class"),
    _rule(_HEAD + _KT_MODS + r"interface[ \t]+(?P<name>\w+)(?P<detail>[^{;\n]*)", "interface"),
    _rule(_HEAD + _KT_MODS + r"object[ \t]+(?P<name>\w+)", "class"),
    _rule(_HEAD + _KT_MODS + r"typealias[ \t]+(?P<name>\w+)", "type"),
]

# ---- Swift ----
_SWIFT_MODS = (
    r"(?:(?:@\w+(?:\([^)\n]*\))?|public|private|internal|fileprivate|open|static|class|final|"
    r"override|mutating|nonmutating|convenience|required|dynamic|lazy|weak|unowned|indirect)"
    r"[ \t]+)*"
)
_SWIFT_RULES = [
    _rule(
        _HEAD + _SWIFT_MODS + r"func[ \t]+(?P<name>\w+)[^(\n]*\((?P<args>[^;{)]*)\)",
        "function",
        args_group="args",
        kind_if_container="method",
    ),
    _rule(_HEAD + _SWIFT_MODS + r"protocol[ \t]+(?P<name>\w+)", "interface"),
    _rule(_HEAD + _SWIFT_MODS + r"struct[ \t]+(?P<name>\w+)(?P<detail>[^{;\n]*)", "struct"),
    _rule(_HEAD + _SWIFT_MODS + r"enum[ \t]+(?P<name>\w+)(?P<detail>[^{;\n]*)", "enum"),
    _rule(_HEAD + _SWIFT_MODS + r"class[ \t]+(?P<name>\w+)(?P<detail>[^{;\n]*)", "class"),
    # extension 是给已有类型加成员，语义上最接近 rust 的 impl
    _rule(_HEAD + _SWIFT_MODS + r"extension[ \t]+(?P<name>\w+)", "impl", container_from_name=True),
    _rule(_HEAD + _SWIFT_MODS + r"typealias[ \t]+(?P<name>\w+)", "type"),
]

# ---- PHP ----
_PHP_MODS = r"(?:(?:public|private|protected|static|abstract|final|readonly|var)[ \t]+)*"
_PHP_RULES = [
    _rule(_HEAD + r"namespace[ \t]+(?P<name>[\w\\]+)", "namespace"),
    _rule(_HEAD + _PHP_MODS + r"class[ \t]+(?P<name>\w+)(?P<detail>[^{;\n]*)", "class"),
    _rule(_HEAD + _PHP_MODS + r"interface[ \t]+(?P<name>\w+)(?P<detail>[^{;\n]*)", "interface"),
    _rule(_HEAD + _PHP_MODS + r"trait[ \t]+(?P<name>\w+)(?P<detail>[^{;\n]*)", "trait"),
    _rule(_HEAD + _PHP_MODS + r"enum[ \t]+(?P<name>\w+)", "enum"),
    _rule(
        _HEAD + _PHP_MODS + r"function[ \t]+&?[ \t]*(?P<name>\w+)[ \t]*\((?P<args>[^;{)]*)\)",
        "function",
        args_group="args",
        kind_if_container="method",
    ),
]

# ---- Ruby ----
# 没有花括号，用 def/class/module … end 配对
_RUBY_RULES = [
    _rule(_HEAD + r"class[ \t]+(?P<name>[\w:]+)(?P<detail>[^;\n#]*)", "class", end_style="ruby"),
    _rule(_HEAD + r"module[ \t]+(?P<name>[\w:]+)", "namespace", end_style="ruby"),
    _rule(
        _HEAD + r"def[ \t]+(?P<name>[\w?!=]+)[ \t]*(?:\((?P<args>[^)\n]*)\))?",
        "function",
        args_group="args",
        kind_if_container="method",
        end_style="ruby",
    ),
]

# ---- Scala ----
_SCALA_MODS = (
    r"(?:(?:private|protected|final|sealed|abstract|implicit|lazy|override|case|open|"
    r"transparent|inline)[ \t]+)*"
)
_SCALA_RULES = [
    # Scala 3 的 `package` 没有块，找 `{` 会跨到后面的类上去
    _rule(_HEAD + r"package[ \t]+(?P<name>[\w.]+)", "namespace", end_style="line"),
    _rule(_HEAD + _SCALA_MODS + r"trait[ \t]+(?P<name>\w+)(?P<detail>[^{;\n]*)", "trait"),
    _rule(
        _HEAD + _SCALA_MODS + r"(?:class|object)[ \t]+(?P<name>\w+)(?P<detail>[^{;\n]*)",
        "class",
    ),
    _rule(
        _HEAD + _SCALA_MODS + r"def[ \t]+(?P<name>[\w$]+)[^(\n]*\((?P<args>[^;{)]*)\)",
        "function",
        args_group="args",
        kind_if_container="method",
    ),
]

# ---- Objective-C ----
# @interface/@implementation 以 @end 收尾；方法体仍是花括号，所以两种 end_style 都要
_OBJC_RULES = [
    _rule(_HEAD + r"@interface[ \t]+(?P<name>\w+)", "class", end_style="objc"),
    _rule(_HEAD + r"@implementation[ \t]+(?P<name>\w+)", "class", end_style="objc"),
    _rule(_HEAD + r"@protocol[ \t]+(?P<name>\w+)", "interface", end_style="objc"),
    _rule(
        _HEAD + r"[-+][ \t]*(?P<detail>\([^)\n]*\))[ \t]*(?P<name>\w+)",
        "method",
        detail_group="detail",
    ),
]

_LANG_RULES: dict[str, list[_Rule]] = {
    "java": _JAVA_RULES,
    "c": _C_RULES,
    "cpp": _C_RULES,
    "csharp": _CSHARP_RULES,
    "go": _GO_RULES,
    "rust": _RUST_RULES,
    "javascript": _JS_RULES,
    "typescript": _TS_RULES,
    "kotlin": _KT_RULES,
    "swift": _SWIFT_RULES,
    "php": _PHP_RULES,
    "ruby": _RUBY_RULES,
    "scala": _SCALA_RULES,
    "objectivec": _OBJC_RULES,
}


def _is_noise_line(line: str) -> bool:
    """整行像注释就不要在里面找符号。

    正则匹配难免扫到注释里的示例代码（docstring 里写 `class Foo {` 太常见了），
    这里按"行首特征"做最小代价的过滤，不解析词法。
    """
    stripped = line.strip()
    if not stripped:
        return True
    return stripped.startswith(("//", "#", "/*", "*"))


def _rule_detail(rule: _Rule, groups: dict[str, str | None]) -> str:
    """按规则决定 detail：优先参数串，其次补充说明。

    ``detail_group`` 留空时默认取名为 ``detail`` 的组——绝大多数规则都叫这个名字，
    每条都显式写一遍既啰嗦又容易漏（漏了 detail 就永远是空串）。
    """
    if rule.args_group:
        args = groups.get(rule.args_group)
        if args is not None:
            return "(" + _squash(args) + ")"
    name = rule.detail_group or "detail"
    raw = groups.get(name) or ""
    if raw.strip():
        return _squash(raw)
    return ""


def _rule_end_line(
    text: str,
    lines: list[str],
    offsets: list[int],
    match: re.Match[str],
    rule: _Rule,
    line: int,
) -> int:
    """按规则的 end_style 求结束行，求不到就用起始行。"""
    if rule.end_style == "line":
        # 声明式规则（Go 的 type 别名、Scala 3 的 package）本来就没有块，
        # 硬去找 `{` 会把它后面那个函数的函数体算成自己的范围
        return line
    end: int | None
    if rule.end_style == "ruby":
        end = _keyword_block_end(lines, line, _RUBY_OPEN_RE, _RUBY_END_RE, _RUBY_DO_RE)
    elif rule.end_style == "objc":
        end = _keyword_block_end(lines, line, _OBJC_OPEN_RE, _OBJC_END_RE)
    else:
        end = _brace_block_end(text, match.end(), offsets)
    if end is None or end < line:
        return line
    return end


def _assign_nested_containers(items: list[tuple[Symbol, _Rule]]) -> None:
    """给没写清归属的符号补 container（取最内层的外层符号名）。

    这样 Rust 的 ``fn`` 能落到 ``impl Foo`` 上、Java/C# 方法能落到类上，代价是
    O(n²) 的包含判断——单文件符号量最多几百个，可以接受。
    正则已经给出 container 的（比如 Go 的接收者）不覆盖，因为它比嵌套判断更准。
    """
    for index, (symbol, rule) in enumerate(items):
        if symbol.container:
            continue
        best: Symbol | None = None
        for other_index, (parent, _) in enumerate(items):
            if other_index == index:
                continue
            if parent.line < symbol.line <= parent.end_line:
                # 起点最靠后（并列时范围最小）= 最内层
                if best is None or (parent.line, -parent.end_line) > (best.line, -best.end_line):
                    best = parent
        if best is not None:
            kind = rule.kind_if_container or symbol.kind
            items[index] = (replace(symbol, container=best.name, kind=kind), rule)


def _extract_regex_symbols(text: str, rules: list[_Rule]) -> list[Symbol]:
    """按规则表提取符号（非 Python 语言）。"""
    offsets = _line_offsets(text)
    lines = text.split("\n")
    items: list[tuple[Symbol, _Rule]] = []
    seen: set[tuple[int, str]] = set()
    for rule in rules:
        for match in rule.pattern.finditer(text):
            groups = match.groupdict()
            name = (groups.get("name") or "").strip()
            if not name:
                continue
            line = _line_of(offsets, match.start())
            if _is_noise_line(lines[line - 1] if line <= len(lines) else ""):
                continue
            # 同一行同名只报一个：靠规则表顺序决定 kind 优先级（具体的排在泛化的前面）
            if (line, name) in seen:
                continue
            seen.add((line, name))
            container = ""
            if rule.container_from_name:
                name = container = _short_name(name)
            elif rule.container_group:
                container = _short_name(groups.get(rule.container_group) or "")
            detail = _rule_detail(rule, groups)
            end = _rule_end_line(text, lines, offsets, match, rule, line)
            # Go 接收者这类"正则直接给出归属"的情况也要升级 kind，
            # 否则 `func (r *T) Name(` 会被当成普通函数
            kind = rule.kind_if_container if (rule.kind_if_container and container) else rule.kind
            items.append((Symbol(kind, name, line, end, container, detail), rule))
    _assign_nested_containers(items)
    # 规则是按语言分组的，输出必须回到源码顺序
    items.sort(key=lambda item: (item[0].line, item[0].name))
    return [symbol for symbol, _ in items]


# ============ 对外：符号提取 ============


def extract_symbols(path: str) -> list[Symbol]:
    """提取文件的顶层符号（含类内成员、嵌套函数）。

    任何异常都不向外抛：这是给 LLM 的工具，读一个坏文件不该让整轮对话失败。
    """
    language = language_of(path)
    if language is None:
        return []
    if language == "python":
        return _python_symbols(path)
    rules = _LANG_RULES.get(language)
    if not rules:
        return []
    text = _read_text(path)
    if text is None:
        return []
    try:
        return _extract_regex_symbols(text, rules)
    except Exception:
        # 正则回溯、畸形文件等极端情况：返回空也比炸掉调用方好
        return []


def _display_label(symbol: Symbol, language: str) -> str:
    """Python 沿用它原来的 `def` 写法，其它语言直接用 kind（function/method/class…）。"""
    if language == "python" and symbol.kind in ("function", "method"):
        return "def"
    return symbol.kind


def _with_detail(name: str, detail: str) -> str:
    """拼名字和补充信息：参数串自带括号，`extends Base` 这种需要补个空格。"""
    if not detail:
        return name
    return name + detail if detail.startswith("(") else f"{name} {detail}"


def _listed_symbol_line(symbol: Symbol, language: str) -> str:
    """`  L12: class Foo` / `    L20: def bar(a, b)`；缩进体现 container。"""
    indent = "    " if symbol.container else "  "
    label = _display_label(symbol, language)
    return f"{indent}L{symbol.line}: {label} {_with_detail(symbol.name, symbol.detail)}"


def list_symbols_text(path: str, max_symbols: int = 300) -> str:
    """给 LLM 看的符号清单（多语言版）。"""
    language = language_of(path) or "未知"
    symbols = extract_symbols(path)
    header = f"📄 {path} 的符号（{language}）："
    if not symbols:
        return header + "\n（无符号）"
    limit = max_symbols if max_symbols > 0 else len(symbols)
    shown = symbols[:limit]
    rows = [header]
    rows += [_listed_symbol_line(symbol, language) for symbol in shown]
    if len(shown) < len(symbols):
        rows.append(f"  … 仅显示前 {len(shown)} 个（共 {len(symbols)} 个符号）")
    return "\n".join(rows)


def _matches_name(symbol: Symbol, name: str) -> bool:
    """支持 `bar`，也支持 `Foo.bar` 这种带容器的写法。"""
    if symbol.name == name:
        return True
    return bool(symbol.container) and f"{symbol.container}.{symbol.name}" == name


def find_definition(path: str, name: str) -> tuple[int, int] | None:
    """按名字找定义，返回 (start_line, end_line) 1-based；找不到返回 None。"""
    if not name:
        return None
    language = language_of(path)
    if language is None:
        return None
    symbols = _python_symbols(path) if language == "python" else extract_symbols(path)
    for symbol in symbols:
        if _matches_name(symbol, name):
            return (symbol.line, symbol.end_line)
    return None


# ============ 仓库索引（repo map） ============

# 这些目录里的文件要么不是源码，要么量级大到会淹没真正有用的部分
_SKIP_DIRS = frozenset(
    {
        "__pycache__",
        "node_modules",
        "venv",
        "dist",
        "build",
        "target",
        "out",
        "vendor",
    }
)

_MAX_FILE_BYTES = 1024 * 1024
_MAX_SYMBOLS_PER_FILE = 15
_FOCUS_BONUS = 10
_REFERENCE_WEIGHT = 3


def _iter_source_files(root: str) -> list[str]:
    """递归收集受支持的源文件，顺序确定（目录/文件名都排序）。

    确定性是硬要求：同一份代码必须生成同一份索引，否则没法写测试、也没法做缓存。
    os.walk 本身的顺序依赖文件系统，所以每层都必须显式排序。
    """
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # 原地改 dirnames 才能影响 os.walk 的递归；以 . 开头的目录（.git/.venv/.idea…）全跳过
        dirnames[:] = sorted(
            name for name in dirnames if not name.startswith(".") and name not in _SKIP_DIRS
        )
        for filename in sorted(filenames):
            if os.path.splitext(filename)[1].lower() not in EXT_LANGUAGE:
                continue
            full = os.path.join(dirpath, filename)
            try:
                if os.path.getsize(full) > _MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            found.append(full)
    return found


def _count_languages(files: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for full in files:
        language = EXT_LANGUAGE.get(os.path.splitext(full)[1].lower())
        if language:
            counts[language] = counts.get(language, 0) + 1
    return dict(sorted(counts.items()))


def _reference_counts(files: list[str], stems: list[str]) -> tuple[dict[str, int], list[set[str]]]:
    """统计"模块名被多少个文件提到过"，作为重要度信号。

    做法很糙但够用：把每个文件的 stem（文件名去扩展名）当 token，用一个合并正则扫全文，
    记录每个 token 出现在哪些文件里。一次扫描就能算完全部 token，
    比"每个 token 扫一遍所有文件"少一个数量级。

    返回 (token -> 出现文件数, 每个文件里出现过的 token 集合)。
    """
    tokens = sorted({stem for stem in stems if stem})
    references: dict[str, int] = {}
    found_per_file: list[set[str]] = [set() for _ in files]
    if not tokens:
        return references, found_per_file
    pattern = re.compile(r"\b(?:" + "|".join(re.escape(token) for token in tokens) + r")\b")
    for index, full in enumerate(files):
        text = _read_text(full)
        if text is None:
            continue
        found = set(pattern.findall(text))
        found_per_file[index] = found
        for token in found:
            references[token] = references.get(token, 0) + 1
    return references, found_per_file


def _map_symbol_line(symbol: Symbol) -> str:
    name = f"{symbol.container}.{symbol.name}" if symbol.container else symbol.name
    return f"  L{symbol.line} {symbol.kind} {_with_detail(name, symbol.detail)}"


def build_repo_map(
    root: str, *, max_chars: int = 12000, max_files: int = 400, focus: str = ""
) -> dict:
    """构建仓库索引。

    score = 符号数 + 3 × 被其它文件引用的次数，focus 命中额外 +10；
    排序键是 ``(-score, 相对路径)`` —— 带上路径兜底才能保证同分文件顺序稳定。

    返回字典里几个计数口径要说清楚，不然容易对不上：
    ``files``/``languages`` 描述整个扫描（含被 max_files 挡掉的部分），
    ``symbols`` 是参与索引的文件里提取到的符号总数（不随 max_chars 截断而变），
    ``indexed`` 才是真正写进 ``text`` 的文件数。``truncated`` 表示"有内容没进 text"。
    """
    empty = {
        "text": "",
        "files": 0,
        "indexed": 0,
        "symbols": 0,
        "languages": {},
        "truncated": False,
    }
    if not root or not os.path.isdir(root):
        return dict(empty)

    candidates = _iter_source_files(root)
    languages = _count_languages(candidates)
    limit = max_files if max_files > 0 else len(candidates)
    working = candidates[:limit]
    # 文件太多时只索引前 max_files 个（已按路径排序，所以截断位置也确定）
    truncated = len(candidates) > len(working)
    if not working:
        return {**empty, "files": len(candidates), "languages": languages}

    relatives = [os.path.relpath(full, root).replace(os.sep, "/") for full in working]
    stems = [os.path.splitext(os.path.basename(rel))[0] for rel in relatives]
    references, found_per_file = _reference_counts(working, stems)

    focus_lower = focus.strip().lower()
    entries: list[tuple[int, str, list[Symbol]]] = []
    total_symbols = 0
    for index, full in enumerate(working):
        symbols = extract_symbols(full)
        total_symbols += len(symbols)
        stem = stems[index]
        # 减去"自己提到自己"，让分数只反映外部引用
        external = references.get(stem, 0) - (1 if stem in found_per_file[index] else 0)
        score = len(symbols) + _REFERENCE_WEIGHT * max(external, 0)
        if focus_lower and (
            focus_lower in relatives[index].lower()
            or any(focus_lower in symbol.name.lower() for symbol in symbols)
        ):
            score += _FOCUS_BONUS
        entries.append((score, relatives[index], symbols))
    entries.sort(key=lambda entry: (-entry[0], entry[1]))

    parts: list[str] = []
    used = 0
    indexed = 0
    stopped = False
    for _score, relative, symbols in entries:
        # 顶层符号优先展示：一个类里几十个方法，全列出来会把真正重要的入口挤掉
        ordered = sorted(symbols, key=lambda symbol: (bool(symbol.container), symbol.line))
        language = EXT_LANGUAGE.get(os.path.splitext(relative)[1].lower(), "未知")
        block_lines = [f"{relative}  ({language}, {len(symbols)} 符号)"]
        block_lines += [_map_symbol_line(symbol) for symbol in ordered[:_MAX_SYMBOLS_PER_FILE]]
        separator = 2 if parts else 0
        remaining = max_chars - used - separator
        block = "\n".join(block_lines)
        if len(block) <= remaining:
            if separator:
                parts.append("\n" * separator)
            parts.append(block)
            used += separator + len(block)
            indexed += 1
            continue
        # 放不下整个文件：只允许最后这个文件按行截断，之后立刻停
        kept: list[str] = []
        length = 0
        for line in block_lines:
            extra = len(line) + (1 if kept else 0)
            if length + extra > remaining:
                break
            kept.append(line)
            length += extra
        if kept:
            if separator:
                parts.append("\n" * separator)
            parts.append("\n".join(kept))
            used += separator + length
            indexed += 1
        stopped = True
        break

    return {
        "text": "".join(parts),
        "files": len(candidates),
        "indexed": indexed,
        "symbols": total_symbols,
        "languages": languages,
        "truncated": truncated or stopped,
    }


def detect_language_summary(root: str, max_files: int = 400) -> dict[str, int]:
    """返回 {语言: 文件数}，供 project_info 之类的工具展示。跳过目录规则同上。"""
    if not root or not os.path.isdir(root):
        return {}
    counts: dict[str, int] = {}
    total = 0
    for full in _iter_source_files(root):
        language = EXT_LANGUAGE.get(os.path.splitext(full)[1].lower())
        if language is None:
            continue
        counts[language] = counts.get(language, 0) + 1
        total += 1
        if max_files > 0 and total >= max_files:
            break
    return dict(sorted(counts.items()))
