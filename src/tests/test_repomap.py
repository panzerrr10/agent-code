"""多语言符号提取与代码库索引（repomap）。

对应改动前的两个真问题：

1. **代码理解工具是 Python-only**：``list_symbols`` / ``find_references`` /
   ``get_function_body`` 全部 ``import ast``，Java / C / C++ 一律不可用。
2. **async 函数完全不可见**：原实现用 ``isinstance(node, ast.FunctionDef)`` 判断，
   而 ``ast.AsyncFunctionDef`` **不是** ``ast.FunctionDef`` 的子类 ——
   现代 Python 项目（FastAPI / aiohttp）里所有 async 函数在符号列表里消失。
"""

import os

import pytest

import repomap

# ============ 语言识别与文本判定 ============


@pytest.mark.parametrize(
    "name,lang",
    [
        ("a.py", "python"),
        ("A.PY", "python"),
        ("Main.java", "java"),
        ("lib.c", "c"),
        ("lib.h", "c"),
        ("lib.cpp", "cpp"),
        ("lib.hpp", "cpp"),
        ("X.cs", "csharp"),
        ("main.go", "go"),
        ("lib.rs", "rust"),
        ("app.js", "javascript"),
        ("app.tsx", "typescript"),
        ("M.kt", "kotlin"),
        ("S.swift", "swift"),
        ("i.php", "php"),
        ("a.rb", "ruby"),
        ("Makefile", None),
        ("README", None),
    ],
)
def test_language_of(name, lang):
    assert repomap.language_of(name) == lang


def test_is_probably_text(tmp_path):
    py = tmp_path / "a.py"
    py.write_text("print(1)\n", encoding="utf-8")
    assert repomap.is_probably_text(str(py)) is True

    md = tmp_path / "notes.md"
    md.write_text("# hi\n", encoding="utf-8")
    assert repomap.is_probably_text(str(md)) is True

    png = tmp_path / "x.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    assert repomap.is_probably_text(str(png)) is False

    # 文件名像文本但内容是二进制 —— 必须靠空字节判定兜住
    fake = tmp_path / "trap.py"
    fake.write_bytes(b"\x00\x01\x02binary")
    assert repomap.is_probably_text(str(fake)) is False

    assert repomap.is_probably_text(str(tmp_path / "nope.py")) is False
    assert repomap.is_probably_text(str(tmp_path)) is False


# ============ Python：async 必须可见（本次修复的真 bug）============


def test_async_function_is_extracted(tmp_path):
    f = tmp_path / "a.py"
    f.write_text(
        "async def fetch(url):\n"
        "    return url\n"
        "\n"
        "class Svc:\n"
        "    async def run(self):\n"
        "        pass\n"
        "    def sync(self):\n"
        "        pass\n",
        encoding="utf-8",
    )
    names = {s.name for s in repomap.extract_symbols(str(f))}
    assert "fetch" in names, "async 顶层函数必须可见（AsyncFunctionDef 不是 FunctionDef 子类）"
    assert "run" in names, "async 方法必须可见"
    assert "sync" in names
    assert "Svc" in names


def test_nested_function_and_inner_class(tmp_path):
    f = tmp_path / "a.py"
    f.write_text(
        "def outer():\n"
        "    def inner():\n"
        "        pass\n"
        "    class Inner:\n"
        "        def deep(self):\n"
        "            pass\n",
        encoding="utf-8",
    )
    symbols = repomap.extract_symbols(str(f))
    names = {s.name for s in symbols}
    assert {"outer", "inner", "Inner", "deep"} <= names


def test_python_method_container_and_lines(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("class A:\n    def m(self):\n        return 1\n", encoding="utf-8")
    by_name = {s.name: s for s in repomap.extract_symbols(str(f))}
    assert by_name["m"].container == "A"
    assert (by_name["m"].line, by_name["m"].end_line) == (2, 3)


def test_broken_python_returns_empty_not_crash(tmp_path):
    f = tmp_path / "bad.py"
    f.write_text("def broken(:\n", encoding="utf-8")
    assert repomap.extract_symbols(str(f)) == []


# ============ 多语言（Java / C / C++ / Go / Rust / TS）============


def test_java_symbols(tmp_path):
    f = tmp_path / "Main.java"
    f.write_text(
        "package demo;\n"
        "public class Main {\n"
        "    public int add(int a, int b) { return a + b; }\n"
        "}\n"
        "interface Callback { void onDone(); }\n"
        "enum Color { RED }\n",
        encoding="utf-8",
    )
    names = {s.name for s in repomap.extract_symbols(str(f))}
    assert {"Main", "add", "Callback", "Color"} <= names


def test_c_symbols(tmp_path):
    f = tmp_path / "lib.c"
    f.write_text(
        "#include <stdio.h>\n"
        "struct Point { int x; int y; };\n"
        "int add(int a, int b)\n"
        "{\n"
        "    return a + b;\n"
        "}\n",
        encoding="utf-8",
    )
    names = {s.name for s in repomap.extract_symbols(str(f))}
    assert {"Point", "add"} <= names


def test_cpp_symbols(tmp_path):
    f = tmp_path / "lib.cpp"
    f.write_text(
        "namespace ns {\n"
        "class Widget {\n"
        "public:\n"
        "    void draw() { }\n"
        "};\n"
        "}\n"
        "void free_fn(int x) { }\n",
        encoding="utf-8",
    )
    names = {s.name for s in repomap.extract_symbols(str(f))}
    assert {"ns", "Widget", "draw", "free_fn"} <= names


def test_go_symbols(tmp_path):
    f = tmp_path / "main.go"
    f.write_text(
        "package main\n"
        "func Top(a int) int { return a }\n"
        "type T struct { X int }\n"
        "func (r *T) Move() {}\n",
        encoding="utf-8",
    )
    by_name = {s.name: s for s in repomap.extract_symbols(str(f))}
    assert {"Top", "T", "Move"} <= set(by_name)
    assert by_name["Move"].container == "T", "Go 的方法要归到接收者类型"


def test_rust_symbols(tmp_path):
    """真实 Rust 写法（多行 impl）。

    已知召回缺口（见 repomap 的说明）：``impl S { fn m(&self) {} }`` 这种
    **同一行的紧凑写法**里的方法提不到 —— 提取器按「fn 出现在行首」匹配。
    这是刻意取舍：宁可少提，也不要为了覆盖紧凑写法而误报。
    """
    f = tmp_path / "lib.rs"
    f.write_text(
        "fn top(x: i32) -> i32 { x }\n"
        "struct S { a: i32 }\n"
        "impl S {\n"
        "    fn m(&self) {}\n"
        "}\n"
        "trait Tr {\n"
        "    fn t(&self);\n"
        "}\n",
        encoding="utf-8",
    )
    names = {s.name for s in repomap.extract_symbols(str(f))}
    assert {"top", "S", "m", "Tr"} <= names


def test_typescript_symbols(tmp_path):
    f = tmp_path / "app.ts"
    f.write_text(
        "export interface Options { a: number }\n"
        "export class Widget { m() {} }\n"
        "export function build(o: Options): void {}\n"
        "export const run = (o: Options) => o;\n",
        encoding="utf-8",
    )
    names = {s.name for s in repomap.extract_symbols(str(f))}
    assert {"Options", "Widget", "build", "run"} <= names


def test_unknown_extension_returns_empty(tmp_path):
    f = tmp_path / "data.xyz"
    f.write_text("whatever\n", encoding="utf-8")
    assert repomap.extract_symbols(str(f)) == []


def test_nonexistent_file_returns_empty(tmp_path):
    assert repomap.extract_symbols(str(tmp_path / "nope.java")) == []


# ============ list_symbols_text / find_definition ============


def test_list_symbols_text_mentions_language_and_lines(tmp_path):
    f = tmp_path / "A.java"
    f.write_text("class A {\n    void m() {}\n}\n", encoding="utf-8")
    out = repomap.list_symbols_text(str(f))
    assert "A.java" in out
    assert "java" in out
    assert "L1" in out


def test_list_symbols_text_no_symbols(tmp_path):
    f = tmp_path / "Empty.java"
    f.write_text("// 只有注释\n", encoding="utf-8")
    assert "无符号" in repomap.list_symbols_text(str(f))


def test_find_definition_python_async(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("async def target():\n    return 1\n", encoding="utf-8")
    assert repomap.find_definition(str(f), "target") == (1, 2)


def test_find_definition_multilang(tmp_path):
    f = tmp_path / "A.java"
    f.write_text("class A {\n    void m() {\n        int x = 1;\n    }\n}\n", encoding="utf-8")
    span = repomap.find_definition(str(f), "m")
    assert span is not None
    assert span[0] == 2


def test_find_definition_missing(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("def a():\n    pass\n", encoding="utf-8")
    assert repomap.find_definition(str(f), "nope") is None


# ============ build_repo_map ============


def _make_repo(root):
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "Main.java").write_text(
        "class Main { void run() {} }\n", encoding="utf-8"
    )
    (root / "src" / "Util.java").write_text(
        "class Util { static int add(int a, int b) { return a + b; } }\n", encoding="utf-8"
    )
    (root / "src" / "app.py").write_text(
        "import os\n\n\ndef main():\n    pass\n", encoding="utf-8"
    )
    (root / "README.md").write_text("# 说明\n", encoding="utf-8")
    (root / "logo.png").write_bytes(b"\x89PNG\x00\x00")
    (root / "node_modules").mkdir(exist_ok=True)
    (root / "node_modules" / "junk.js").write_text("function junk(){}\n", encoding="utf-8")
    (root / ".git").mkdir(exist_ok=True)
    (root / ".git" / "hooks.js").write_text("function h(){}\n", encoding="utf-8")


def test_build_repo_map_basic(tmp_path):
    _make_repo(tmp_path)
    data = repomap.build_repo_map(str(tmp_path))
    assert data["files"] == 3, "只算源码文件；README/png/node_modules/.git 都要排除"
    assert data["languages"] == {"java": 2, "python": 1}
    assert data["symbols"] >= 3
    assert "Main.java" in data["text"]
    assert "junk.js" not in data["text"]
    assert "hooks.js" not in data["text"]


def test_build_repo_map_is_deterministic(tmp_path):
    _make_repo(tmp_path)
    a = repomap.build_repo_map(str(tmp_path))
    b = repomap.build_repo_map(str(tmp_path))
    assert a["text"] == b["text"], "同输入必须同输出（否则没法写断言、也没法对比）"
    assert a == b


def test_build_repo_map_respects_max_chars(tmp_path):
    _make_repo(tmp_path)
    data = repomap.build_repo_map(str(tmp_path), max_chars=80)
    assert len(data["text"]) <= 200
    assert data["truncated"] is True


def test_build_repo_map_focus_boosts_matching_file(tmp_path):
    _make_repo(tmp_path)
    data = repomap.build_repo_map(str(tmp_path), focus="Util")
    # 命中 focus 的文件必须出现在索引里
    assert "Util.java" in data["text"]


@pytest.mark.parametrize("bad", ["/does/not/exist", ""])
def test_build_repo_map_missing_root(bad):
    data = repomap.build_repo_map(bad)
    assert data["files"] == 0
    assert data["text"] == ""


def test_build_repo_map_root_is_file(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    assert repomap.build_repo_map(str(f))["files"] == 0


def test_detect_language_summary(tmp_path):
    _make_repo(tmp_path)
    assert repomap.detect_language_summary(str(tmp_path)) == {"java": 2, "python": 1}


def test_binary_extension_sets_do_not_overlap():
    """文本集合与二进制集合不能同时命中，否则 search_files 会漏文件。"""
    assert not (repomap.TEXT_EXTENSIONS & repomap.BINARY_EXTENSIONS)


def test_language_extensions_are_all_searchable():
    """所有语言的源码后缀都必须在可搜索范围里（这正是原来 search_files 的漏洞）。"""
    missing = [ext for ext in repomap.EXT_LANGUAGE if ext not in repomap.TEXT_EXTENSIONS]
    assert not missing, f"这些语言后缀搜不到：{missing}"


def test_repo_map_does_not_touch_real_project():
    """索引本仓库必须能跑通且不抛异常（冒烟）。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data = repomap.build_repo_map(root, max_chars=3000)
    assert data["files"] > 5
    assert "tools.py" in data["text"]
