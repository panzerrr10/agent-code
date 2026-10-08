"""核心工具的集成测试（真实读写临时目录，不访问网络）。

与 tests/test_security_fixes.py 的区别：那边测「安全边界」，这边测「功能是否真的好用」——
文件读写改删、备份/回收站、AST 代码理解、记忆、Git、测试运行器的正常路径。
"""

import os
import re
import shutil
import subprocess
import tempfile

import pytest

import runtime
import tools
import trash

HOME = os.path.expanduser("~")
PY_SAMPLE = '''
def add(a, b):
    """求和。"""
    return a + b


class Calc:
    def mul(self, x, y):
        return x * y
'''


@pytest.fixture
def proj(monkeypatch):
    """在 home 下建临时项目目录。

    工具的路径白名单只放行 home，所以临时目录必须建在 home 里；
    同时把 TRASH_DIR 指到临时目录内，避免测试往仓库的 data/.trash 里堆备份。
    """
    d = tempfile.mkdtemp(prefix=".agentcode-test-", dir=HOME)
    monkeypatch.setattr(trash, "TRASH_DIR", os.path.join(d, ".trash"))
    runtime.reset_all()
    runtime.set_session_id("tools-test")
    runtime.set_project_root(d)
    yield d
    runtime.reset_all()
    runtime.set_session_id(None)
    shutil.rmtree(d, ignore_errors=True)


# ============ 文件读写 ============


def test_create_file_autocreates_parent_and_refuses_overwrite(proj):
    assert "已创建" in tools.create_file("a/b/c.txt", "hello 世界")
    assert os.path.isfile(os.path.join(proj, "a", "b", "c.txt"))
    assert "hello 世界" in tools.read_file("a/b/c.txt")

    assert "已存在" in tools.create_file("a/b/c.txt", "覆盖试试")
    assert "hello 世界" in tools.read_file("a/b/c.txt")


def test_read_file_range_boundaries(proj):
    tools.create_file("n.txt", "\n".join(f"L{i}" for i in range(1, 11)))
    out = tools.read_file_range("n.txt", 3, 5)
    assert "L3" in out and "L5" in out
    assert "L6" not in out
    assert "共 10 行" in out

    assert "起始行 > 结束行" in tools.read_file_range("n.txt", 9, 2)


def test_write_file_backs_up_original(proj):
    tools.create_file("f.txt", "OLD")
    r = tools.write_file("f.txt", "NEW")
    assert "已写入" in r
    assert "NEW" in tools.read_file("f.txt")
    assert "OLD" not in tools.read_file("f.txt")
    backups = os.listdir(trash.TRASH_DIR)
    assert any("backup_f.txt" in b for b in backups), backups


def test_edit_file_requires_unique_match_and_backs_up(proj):
    tools.create_file("e.txt", "x\ny\nx\n")
    assert "找到 2 处" in tools.edit_file("e.txt", "x", "z")
    assert "不匹配" in tools.edit_file("e.txt", "NOT_THERE", "z")
    assert "已替换" in tools.edit_file("e.txt", "y", "Y")
    assert "Y" in tools.read_file("e.txt")
    assert any("edit_e.txt" in b for b in os.listdir(trash.TRASH_DIR))


def test_delete_moves_to_trash_and_refuses_directory(proj):
    tools.create_file("d.txt", "x")
    assert "回收站" in tools.delete_file("d.txt")
    assert not os.path.exists(os.path.join(proj, "d.txt"))
    assert any("d.txt" in f for f in os.listdir(trash.TRASH_DIR))

    os.makedirs(os.path.join(proj, "sub"), exist_ok=True)
    assert "不能删除目录" in tools.delete_file("sub")
    assert "不存在" in tools.delete_file("missing.txt")


def test_path_whitelist_blocks_outside_home(proj):
    assert "拒绝访问" in tools.read_file("/etc/passwd")
    assert "拒绝访问" in tools.create_file("/etc/agent-code-should-not-exist.txt", "x")
    assert not os.path.exists("/etc/agent-code-should-not-exist.txt")


def test_list_dir_search_files_and_file_info(proj):
    tools.create_file("p/one.py", "print(1)")
    tools.create_file("p/two.txt", "这里有个 needle 关键词")

    listing = tools.list_dir("p")
    assert "one.py" in listing and "two.txt" in listing

    found = tools.search_files("needle", "p")
    assert "two.txt" in found

    info = tools.get_file_info("p/one.py")
    assert "one.py" in info or "文件" in info
    assert "大小" in info and "修改时间" in info


def test_project_info_detects_project_type(proj):
    tools.create_file("requirements.txt", "flask")
    out = tools.project_info()
    assert proj in out
    assert "Python (requirements)" in out


# ============ AST 代码理解 ============


def test_ast_tools(proj):
    tools.create_file("m.py", PY_SAMPLE)

    syms = tools.list_symbols("m.py")
    assert "def add(a, b)" in syms
    assert "class Calc" in syms
    assert "def mul(self, x, y)" in syms

    body = tools.get_function_body("m.py", "add")
    assert "return a + b" in body
    assert "class Calc" not in body  # 只取目标函数

    refs = tools.find_references("add", ".")
    assert "m.py" in refs

    assert "未找到函数" in tools.get_function_body("m.py", "not_exist")


def test_list_symbols_reports_syntax_error(proj):
    tools.create_file("bad.py", "def broken(:\n")
    assert "语法错误" in tools.list_symbols("bad.py")


# ============ 记忆 ============


def test_memory_tools_roundtrip():
    r = tools.save_memory("单元测试用记忆-可安全删除")
    assert "已保存记忆" in r
    mid = int(re.search(r"ID: (\d+)", r).group(1))

    assert "单元测试用记忆-可安全删除" in tools.list_memories()
    assert "已删除" in tools.delete_memory(mid)
    assert "单元测试用记忆-可安全删除" not in tools.list_memories()


# ============ 知识库检索（打桩，避免真实 embedding 请求）============


def test_search_knowledge_two_tier_threshold(monkeypatch):
    import rag

    thresholds = []

    def fake_search(query, top_k=3, min_score=0.4, api_key=None, base_url=None):
        thresholds.append(min_score)
        if min_score < 0.2:  # 只有降低阈值后才命中
            return [{"text": "片段正文", "filename": "手册.md", "score": 0.2}]
        return []

    monkeypatch.setattr(rag, "search_similar", fake_search)

    out = tools.search_knowledge("某个关键词")
    assert "手册.md" in out and "片段正文" in out
    # 先 0.4 严格召回，未命中再降到 0.15 兜底
    assert thresholds == [0.4, 0.15]


def test_search_knowledge_reports_empty(monkeypatch):
    import rag

    monkeypatch.setattr(rag, "search_similar", lambda *a, **k: [])
    assert "未在知识库中找到" in tools.search_knowledge("不存在的东西")


# ============ Git（临时仓库）============


@pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")
def test_git_tools_in_temp_repo(proj):
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=proj, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=proj, check=True)

    tools.create_file("a.txt", "hello")
    assert "a.txt" in tools.git_status()

    assert "退出码】0" in tools.git_commit("初次提交")
    assert "初次提交" in tools.git_log(5)

    tools.write_file("a.txt", "changed")
    assert "a.txt" in tools.git_diff()

    # 注入防护仍然生效
    assert "非法" in tools.git_rollback("HEAD~1; rm -rf /")


def test_git_tools_report_missing_repo_gracefully(proj):
    # 临时目录不是 git 仓库：应当返回错误信息而不是抛异常
    out = tools.git_status()
    assert "不是 Git 仓库" in out or "退出码" in out


# ============ 测试运行器 ============


def test_run_tests_rejects_path_outside_project(proj):
    assert "必须位于项目目录内" in tools.run_tests("/etc")


def test_run_tests_runs_sample_test_file(proj):
    # 用 unittest.TestCase 写样例：pytest 与 unittest 回退路径都能收集到
    tools.create_file(
        "test_sample_ok.py",
        "import unittest\n"
        "\n"
        "\n"
        "class TestSample(unittest.TestCase):\n"
        "    def test_ok(self):\n"
        "        self.assertEqual(1 + 1, 2)\n",
    )
    out = tools.run_tests("test_sample_ok.py")
    assert "退出码】0" in out, out[:500]


def test_run_tests_reports_failure(proj):
    tools.create_file(
        "test_sample_bad.py",
        "import unittest\n"
        "\n"
        "\n"
        "class TestBad(unittest.TestCase):\n"
        "    def test_bad(self):\n"
        "        self.assertEqual(1, 2)\n",
    )
    out = tools.run_tests("test_sample_bad.py")
    assert "退出码】0" not in out, "失败的测试不应报成功"
    assert "AssertionError" in out or "FAILED" in out or "退出码】1" in out
