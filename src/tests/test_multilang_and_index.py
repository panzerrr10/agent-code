"""多语言搜索 + 代码库索引注入 + 多语言符号工具（tools 层）。

改动前的实测漏洞：
``search_files`` 用的是**扩展名白名单**，且漏掉了整个现代技术栈
（.ts/.tsx/.vue/.go/.rs/.java/.kt/.c/.cpp/.cs/.php/.rb/.sql/.sh）。
后果很隐蔽：在 TypeScript / Go / Java 项目里搜索会**静默返回「未找到」**，
模型据此判断「项目里没有这个东西」，然后基于错误前提继续干活。
"""

import os
import shutil
import tempfile

import pytest

import repo_context
import runtime
import tools
import trash

HOME = os.path.expanduser("~")


@pytest.fixture
def proj(monkeypatch):
    """home 下的临时项目（工具白名单只放行 home）。"""
    d = tempfile.mkdtemp(prefix=".agentcode-ml-", dir=HOME)
    monkeypatch.setattr(trash, "TRASH_DIR", os.path.join(d, ".trash"))
    runtime.reset_all()
    runtime.set_session_id("ml-test")
    runtime.set_project_root(d)
    yield d
    runtime.reset_all()
    runtime.set_session_id(None)
    shutil.rmtree(d, ignore_errors=True)


def _write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


# ============ search_files 必须覆盖多语言 ============


@pytest.mark.parametrize(
    "rel",
    [
        "src/a.ts",
        "src/b.tsx",
        "src/c.go",
        "src/d.rs",
        "src/E.java",
        "src/f.kt",
        "src/g.c",
        "src/h.cpp",
        "src/i.cs",
        "src/j.php",
        "src/k.rb",
        "src/l.sql",
        "src/m.sh",
        "src/n.vue",
        "src/o.swift",
        "src/p.py",
        "src/q.js",
        "notes.txt",
    ],
)
def test_search_files_covers_language(proj, rel):
    """每种语言的源文件里都放一个独特关键词，必须能搜到。"""
    keyword = "ZQX_UNIQUE_TOKEN_42"
    _write(proj, rel, f"// {keyword}\n")
    out = tools.search_files(keyword)
    assert keyword in out, f"{rel} 搜不到 —— 语言白名单又漏了"
    assert rel in out


def test_search_files_skips_binary(proj):
    _write(proj, "src/real.ts", "// ZQX_BIN_TEST\n")
    with open(os.path.join(proj, "logo.png"), "wb") as f:
        f.write(b"\x89PNG\x00\x00ZQX_BIN_TEST")
    out = tools.search_files("ZQX_BIN_TEST")
    assert "real.ts" in out
    assert "logo.png" not in out


def test_search_files_skips_build_and_hidden_dirs(proj):
    _write(proj, "src/keep.ts", "// ZQX_DIR_TEST\n")
    _write(proj, "node_modules/pkg/x.js", "// ZQX_DIR_TEST\n")
    _write(proj, "dist/x.js", "// ZQX_DIR_TEST\n")
    _write(proj, ".hidden/x.js", "// ZQX_DIR_TEST\n")
    out = tools.search_files("ZQX_DIR_TEST")
    assert "keep.ts" in out
    for bad in ("node_modules", "dist/", ".hidden"):
        assert bad not in out, f"{bad} 不该被搜"


def test_search_files_reports_scanned_count_when_empty(proj):
    _write(proj, "a.py", "print(1)\n")
    out = tools.search_files("绝对不存在的关键词_ZZZ")
    assert "未找到" in out
    # 明确告诉模型「搜了多少文件」，避免它把「没搜到」误解成「不存在」
    assert "已扫描" in out


def test_search_files_marks_truncation(proj):
    for i in range(60):
        _write(proj, f"f{i}.py", "ZZZ_MANY\n")
    out = tools.search_files("ZZZ_MANY", max_results=5)
    assert "上限" in out or "截断" in out


# ============ 多语言符号工具 ============


def test_list_symbols_java(proj):
    _write(proj, "Main.java", "public class Main {\n  public void run() { }\n}\n")
    out = tools.list_symbols("Main.java")
    assert "Main" in out
    assert "run" in out
    assert "java" in out


def test_list_symbols_python_async_visible(proj):
    _write(proj, "a.py", "async def fetch():\n    pass\n")
    out = tools.list_symbols("a.py")
    assert "fetch" in out, "async 函数必须出现在符号列表里（这是本次修复的真 bug）"


def test_list_symbols_still_reports_syntax_error(proj):
    """改用 repomap 之后仍要保住「语法错误」这条诊断信息。"""
    _write(proj, "bad.py", "def broken(:\n")
    assert "语法错误" in tools.list_symbols("bad.py")


def test_list_symbols_rejects_outside_path(proj):
    assert "拒绝" in tools.list_symbols("/etc/passwd")


def test_find_references_multilang(proj):
    _write(proj, "src/Service.java", "class Service { void call() { helper(); } }\n")
    _write(proj, "src/Other.java", "class Other { void helper() {} }\n")
    out = tools.find_references("helper")
    assert "Service.java" in out
    assert "Other.java" in out


def test_get_function_body_multilang(proj):
    _write(proj, "lib.cpp", "int add(int a, int b) {\n    return a + b;\n}\n")
    out = tools.get_function_body("lib.cpp", "add")
    assert "return a + b;" in out
    assert "L1" in out


def test_get_function_body_missing(proj):
    _write(proj, "a.java", "class A {}\n")
    assert "未找到" in tools.get_function_body("a.java", "nope")


# ============ project_info 展示语言分布 ============


def test_project_info_shows_languages(proj):
    _write(proj, "A.java", "class A {}\n")
    _write(proj, "B.java", "class B {}\n")
    _write(proj, "c.py", "x = 1\n")
    out = tools.project_info()
    assert "语言分布" in out
    assert "java 2 文件" in out


# ============ repo_context 注入 ============


def test_repo_context_skips_tiny_project(proj):
    """小项目不注入 —— 直接 list_dir 就够了，注入只占上下文。"""
    _write(proj, "pyproject.toml", "[project]\n")
    _write(proj, "a.py", "x = 1\n")
    msg, data = repo_context.build_system_message(proj)
    assert msg is None
    assert data["text"] == ""


def test_repo_context_injects_for_real_project(proj):
    _write(proj, "pom.xml", "<project/>\n")          # 项目标记
    for i in range(6):
        _write(proj, f"src/M{i}.java", f"class M{i} {{\n  void run() {{}}\n}}\n")
    msg, data = repo_context.build_system_message(proj)
    assert msg is not None
    assert msg["role"] == "system"
    assert "代码库索引" in msg["content"]
    assert "M0.java" in msg["content"]
    assert data["files"] == 6


def test_repo_context_respects_env_off(proj, monkeypatch):
    for i in range(6):
        _write(proj, f"src/M{i}.java", f"class M{i} {{}}\n")
    monkeypatch.setenv("AGENT_REPO_MAP", "0")
    msg, _ = repo_context.build_system_message(proj)
    assert msg is None


def test_repo_context_max_chars_env(proj, monkeypatch):
    _write(proj, "pom.xml", "<project/>\n")
    for i in range(40):
        _write(proj, f"src/M{i}.java", f"class M{i} {{\n  void run() {{}}\n}}\n")
    monkeypatch.setenv("AGENT_REPO_MAP_CHARS", "1000")
    msg, data = repo_context.build_system_message(proj)
    assert msg is not None
    assert data["truncated"] is True
    assert len(msg["content"]) < 4000


def test_repo_context_none_root():
    msg, data = repo_context.build_system_message(None)
    assert msg is None
    assert data["text"] == ""


def test_repo_context_unreadable_root_does_not_raise():
    msg, _ = repo_context.build_system_message("/definitely/not/here")
    assert msg is None


# ============ 「这是项目吗」的闸门（防止索引整个家目录）============


def test_home_like_directory_without_marker_is_skipped(proj):
    """默认工作区是 $HOME。不设闸门的话，索引会把整个家目录的无关项目
    路径与符号塞进每一次请求（实测 5973 个文件）—— 既烧 token 又泄漏信息。
    """
    for i in range(repo_context.MARKERLESS_MAX_FILES + 5):
        _write(proj, f"pkg{i % 8}/f{i}.py", "x = 1\n")
    msg, data = repo_context.build_system_message(proj)
    assert msg is None
    assert data["skipped"] == "not_a_project"


def test_markerless_but_small_directory_is_indexed(proj):
    """没有标记但文件很少 —— 仍然是用户的工作目录，照常索引。"""
    for i in range(6):
        _write(proj, f"f{i}.py", f"def fn{i}():\n    pass\n")
    msg, data = repo_context.build_system_message(proj)
    assert msg is not None
    assert data["skipped"] == ""


@pytest.mark.parametrize("marker", [".git", "pyproject.toml", "package.json", "go.mod", "pom.xml"])
def test_project_markers_enable_index(proj, marker):
    if marker == ".git":
        os.makedirs(os.path.join(proj, ".git"), exist_ok=True)
    else:
        _write(proj, marker, "{}\n")
    for i in range(40):
        _write(proj, f"src/f{i}.py", f"def fn{i}():\n    pass\n")
    msg, _ = repo_context.build_system_message(proj)
    assert msg is not None, f"{marker} 存在时应视为项目并注入索引"


def test_real_home_is_not_indexed():
    """真实 $HOME 必须被闸门挡住（这是本仓库默认的工作区）。"""
    msg, data = repo_context.build_system_message(os.path.expanduser("~"))
    assert msg is None
    assert data["skipped"] in ("not_a_project", "")


def test_count_source_files_is_bounded(proj):
    for i in range(200):
        _write(proj, f"f{i}.py", "x = 1\n")
    # 数到上限就停，不会把整个目录扫完
    assert repo_context._count_source_files(proj, 20) == 21


# ============ 工具集：run_command 必须常驻 ============


@pytest.mark.parametrize(
    "task",
    [
        "做一个 BMI 计算器网页",
        "把这个项目跑起来",
        "安装依赖",
        "编译这个 C++ 项目",
        "mvn package",
        "重构一下登录模块",
        "分析一下这个项目",
    ],
)
def test_run_command_is_always_injected(task):
    """改动前它只靠中文关键词注入，于是「把这个项目跑起来」「编译这个 C++ 项目」
    这类最普通的说法**拿不到 run_command** —— 而 git_commit / git_rollback
    反而是常驻的（危险工具永远在，干活的工具看运气）。

    对多语言项目更是致命：Java 要 mvn/gradle、C/C++ 要 gcc/cmake/make，
    全都靠 run_command。
    """
    names = {t["function"]["name"] for t in tools.get_tools_schema(task=task)}
    assert "run_command" in names, f"「{task}」拿不到 run_command"


def test_run_command_still_subject_to_gates():
    """常驻不等于放行：仍要过权限闸门与命令拦截。"""

    assert tools.is_privileged_tool("run_command") is True
    assert tools.check_banned("rm -rf /") is not None


def test_delete_file_still_keyword_gated():
    """删除类工具仍然按需注入 —— 不是所有工具都该常驻。"""
    names = {t["function"]["name"] for t in tools.get_tools_schema(task="你好")}
    assert "delete_file" not in names
    names2 = {t["function"]["name"] for t in tools.get_tools_schema(task="删掉这个文件")}
    assert "delete_file" in names2
