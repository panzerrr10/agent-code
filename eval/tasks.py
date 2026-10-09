"""评估任务集。

## 为什么需要它

414+ 个单元测试能证明「代码没坏」，但**证明不了「改完到底变好还是变差」**。
改一句系统提示词、换一个工具描述、动一次压缩阈值 —— 这些改动的影响
只能靠**跑真实任务**量出来。

## 任务设计原则

1. **验收必须是确定的**：每条任务一个 `check_cmd`，退出码 0 才算通过。
2. **前置必须失败**：跑 agent 之前先验一次，确保任务不是「本来就过」。
   否则通过率会虚高，而且掩盖了坏掉的验收。
3. **纯标准库**：不装任何依赖，跑得快、不受环境漂移影响。
4. **故意小而集中**：每条只考一种能力，失败时能定位到具体是哪一环弱。

任务本身**不包含**「用哪个模型/多少步」之类的信息 —— 那是运行时参数。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Task:
    id: str
    title: str
    prompt: str
    files: dict
    check_cmd: str
    tags: list = field(default_factory=list)
    timeout: int = 600
    # 跑完后额外要求的检查（可选），例如「不该留下调试代码」
    extra_check: str = ""


ALL_TASKS: list[Task] = [
    # ---------------- 修 bug ----------------
    Task(
        id="fix_failing_test",
        title="修复导致测试失败的 bug",
        prompt="运行项目里的测试，找出失败的原因并修复代码，让测试全部通过。",
        files={
            "calc.py": (
                "def add(a, b):\n"
                "    return a - b          # 这里写错了\n"
                "\n"
                "\n"
                "def mul(a, b):\n"
                "    return a * b\n"
            ),
            "check.py": (
                "from calc import add, mul\n"
                "\n"
                "assert add(2, 3) == 5, add(2, 3)\n"
                "assert add(-1, 1) == 0\n"
                "assert mul(3, 4) == 12\n"
                'print("ok")\n'
            ),
        },
        check_cmd="python3 check.py",
        tags=["bugfix", "test-driven"],
    ),
    Task(
        id="fix_off_by_one",
        title="定位并修复越界错误",
        prompt="项目里的测试报 IndexError，请定位原因并修复。",
        files={
            "window.py": (
                "def last_n(items, n):\n"
                '    """返回最后 n 个元素。"""\n'
                "    return items[len(items) - n - 1:]\n"      # 差一位
            ),
            "check.py": (
                "from window import last_n\n"
                "\n"
                "assert last_n([1, 2, 3, 4, 5], 2) == [4, 5], last_n([1, 2, 3, 4, 5], 2)\n"
                "assert last_n([1, 2, 3], 3) == [1, 2, 3]\n"
                "assert last_n([1, 2], 0) == []\n"
                'print("ok")\n'
            ),
        },
        check_cmd="python3 check.py",
        tags=["bugfix", "debugging"],
    ),
    Task(
        id="fix_import_error",
        title="修复导入错误",
        prompt="运行 main.py 会报导入错误，请修复让程序能正常跑完。",
        files={
            "helpers.py": (
                "def format_full_name(first, last):\n"
                '    return f"{first} {last}".strip()\n'
            ),
            "main.py": (
                "from helpers import format_name\n"
                "\n"
                "if __name__ == '__main__':\n"
                '    print(format_name("Ada", "Lovelace"))\n'
            ),
        },
        check_cmd="python3 main.py",
        tags=["bugfix", "imports"],
    ),

    # ---------------- 实现功能 ----------------
    Task(
        id="implement_function",
        title="按测试实现函数",
        prompt="utils.py 里的 slugify 还没实现，请实现它让测试通过。",
        files={
            "utils.py": (
                "def slugify(text):\n"
                '    """把文本变成小写、连字符分隔的 slug。\n'
                "\n"
                "    规则：转小写；非字母数字的连续字符压成一个 '-'；\n"
                "    去掉首尾的 '-'。\n"
                '    """\n'
                "    raise NotImplementedError\n"
            ),
            "check.py": (
                "from utils import slugify\n"
                "\n"
                "assert slugify('Hello World') == 'hello-world', slugify('Hello World')\n"
                "assert slugify('a  --  b') == 'a-b', slugify('a  --  b')\n"
                "assert slugify('  Hello!  ') == 'hello', slugify('  Hello!  ')\n"
                'print("ok")\n'
            ),
        },
        check_cmd="python3 check.py",
        tags=["feature", "spec-following"],
    ),
    Task(
        id="add_validation",
        title="补输入校验并满足测试",
        prompt="order.py 的 total 函数缺少参数校验，请补上让测试通过。",
        files={
            "order.py": (
                "def total(prices, discount=0):\n"
                '    """算总价。prices 必须是 list，discount 必须在 0~1 之间。"""\n'
                "    s = sum(prices)\n"
                "    return s * (1 - discount)\n"
            ),
            "check.py": (
                "from order import total\n"
                "\n"
                "assert total([10, 20], 0.5) == 15\n"
                "\n"
                "try:\n"
                "    total('abc')\n"
                "    raise AssertionError('非 list 应当抛 TypeError')\n"
                "except TypeError:\n"
                "    pass\n"
                "\n"
                "try:\n"
                "    total([10], 2)\n"
                "    raise AssertionError('越界 discount 应当抛 ValueError')\n"
                "except ValueError:\n"
                "    pass\n"
                'print("ok")\n'
            ),
        },
        check_cmd="python3 check.py",
        tags=["feature", "validation", "spec-following"],
    ),

    # ---------------- 跨文件改动 ----------------
    Task(
        id="multi_file_rename",
        title="跨文件重命名函数",
        prompt=(
            "把 helpers.py 里的 old_name 重命名为 new_name，"
            "并更新所有调用它的地方，最后确保程序能正常运行。"
        ),
        files={
            "helpers.py": (
                "def old_name(x):\n"
                "    return x * 2\n"
            ),
            "a.py": (
                "from helpers import old_name\n"
                "\n"
                "def run_a():\n"
                "    return old_name(1)\n"
            ),
            "b.py": (
                "from helpers import old_name\n"
                "\n"
                "def run_b():\n"
                "    return old_name(2)\n"
            ),
            "main.py": (
                "from a import run_a\n"
                "from b import run_b\n"
                "\n"
                "if __name__ == '__main__':\n"
                "    assert run_a() == 2 and run_b() == 4\n"
                '    print("ok")\n'
            ),
        },
        # 既要求跑通，也要求旧名字彻底消失（不能留半拉子）
        check_cmd="python3 main.py",
        extra_check="! grep -rn 'old_name' --include='*.py' .",
        tags=["refactor", "multi-file"],
    ),
    Task(
        id="extract_function",
        title="统一两处不一致的实现",
        prompt=(
            "parse.py 里有两个函数本应行为一致，但其中一个有 bug。"
            "请把重复的解析逻辑提取成一个共用函数，让两个函数都正确、行为一致。"
        ),
        files={
            "parse.py": (
                "def parse_a(raw):\n"
                "    parts = [p.strip() for p in raw.split(',') if p.strip()]\n"
                "    return [p.upper() for p in parts]\n"
                "\n"
                "\n"
                "def parse_b(raw):\n"
                "    # 少了过滤空串，和 parse_a 行为不一致\n"
                "    parts = [p.strip() for p in raw.split(',')]\n"
                "    return [p.upper() for p in parts]\n"
            ),
            "check.py": (
                "from parse import parse_a, parse_b\n"
                "\n"
                "assert parse_a('a, b ,c') == ['A', 'B', 'C'], parse_a('a, b ,c')\n"
                "assert parse_b('a, b ,c') == ['A', 'B', 'C'], parse_b('a, b ,c')\n"
                "assert parse_a('x,,y') == ['X', 'Y'], parse_a('x,,y')\n"
                "assert parse_b('x,,y') == ['X', 'Y'], parse_b('x,,y')\n"
                "assert parse_a('') == [] and parse_b('') == []\n"
                'print("ok")\n'
            ),
        },
        check_cmd="python3 check.py",
        # 结构要求：必须真的提取出了共用函数（parse.py 里应有 >=3 个函数）
        extra_check=(
            "python3 -c \"import ast,sys; t=ast.parse(open('parse.py').read()); "
            "n=[x.name for x in ast.walk(t) if isinstance(x, ast.FunctionDef)]; "
            "sys.exit(0 if len(n) >= 3 else 1)\""
        ),
        tags=["refactor", "consistency"],
    ),
    # ---------------- 更难：需要坚持与约束遵守 ----------------
    Task(
        id="fix_three_bugs",
        title="一次修三个独立的 bug",
        prompt="项目里有三个不同的 bug，请全部找出来修好，让 check.py 通过。",
        files={
            "a.py": "def first(xs):\n    return xs[1]        # 应为 xs[0]\n",
            "b.py": "def shout(s):\n    return s.upper        # 少了调用括号\n",
            "c.py": "def is_even(n):\n    return n % 2 == 1   # 判断反了\n",
            "check.py": (
                "from a import first\n"
                "from b import shout\n"
                "from c import is_even\n"
                "\n"
                "assert first([9, 8]) == 9, first([9, 8])\n"
                "assert shout('hi') == 'HI', shout('hi')\n"
                "assert is_even(4) is True and is_even(3) is False\n"
                'print("ok")\n'
            ),
        },
        check_cmd="python3 check.py",
        tags=["bugfix", "persistence"],
    ),
    Task(
        id="keep_public_api",
        title="修 bug 但不能改公开接口",
        prompt=(
            "stats.py 的 average 算错了。请修复它，"
            "但**不要改动函数签名**（参数名与个数必须保持不变），否则其他地方会挂。"
        ),
        files={
            "stats.py": (
                "def average(values, ignore_zero=False):\n"
                '    """平均值。ignore_zero=True 时跳过 0。"""\n'
                "    if ignore_zero:\n"
                "        values = [v for v in values if v]\n"
                "    return sum(values) / len(values) + 1     # 多了 +1\n"
            ),
            "check.py": (
                "import inspect\n"
                "\n"
                "from stats import average\n"
                "\n"
                "assert average([2, 4]) == 3, average([2, 4])\n"
                "assert average([0, 4], ignore_zero=True) == 4, average([0, 4], ignore_zero=True)\n"
                "sig = inspect.signature(average)\n"
                "assert list(sig.parameters) == ['values', 'ignore_zero'], list(sig.parameters)\n"
                'print("ok")\n'
            ),
        },
        check_cmd="python3 check.py",
        tags=["bugfix", "constraint-following"],
    ),

    # ---------------- 探索性 ----------------
    Task(
        id="find_bug_in_large_file",
        title="在大文件里定位问题",
        prompt="app_logic.py 里有一个函数算错了，测试会失败。找到并修复它。",
        files={
            "app_logic.py": "\n".join([
                "# 一个稍大一点的模块，用来考察定位能力",
                "import statistics",
                "",
                "",
                "def mean_of(values):",
                '    """平均值。"""',
                "    return statistics.mean(values)",
                "",
                "",
                "def median_of(values):",
                '    """中位数。"""',
                "    s = sorted(values)",
                "    n = len(s)",
                "    if n % 2 == 1:",
                "        return s[n // 2]",
                "    return (s[n // 2] + s[n // 2 - 1]) / 2",
                "",
                "",
                "def variance_of(values):",
                '    """总体方差：每个数与均值差的平方的平均。"""',
                "    m = mean_of(values)",
                "    # 下面这一行漏了平方",
                "    return sum(x - m for x in values) / len(values)",
                "",
                "",
                "def stdev_of(values):",
                '    """总体标准差。"""',
                "    return variance_of(values) ** 0.5",
                "",
                "",
                "def normalize(values):",
                '    """把数值缩放到 0~1。"""',
                "    lo, hi = min(values), max(values)",
                "    if hi == lo:",
                "        return [0.0 for _ in values]",
                "    return [(x - lo) / (hi - lo) for x in values]",
            ]) + "\n",
            "check.py": (
                "from app_logic import normalize, stdev_of, variance_of\n"
                "\n"
                "v = variance_of([2, 4, 4, 4, 5, 5, 7, 9])\n"
                "assert abs(v - 4.0) < 1e-9, v\n"
                "s = stdev_of([2, 4, 4, 4, 5, 5, 7, 9])\n"
                "assert abs(s - 2.0) < 1e-9, s\n"
                "assert normalize([0, 5, 10]) == [0.0, 0.5, 1.0]\n"
                'print("ok")\n'
            ),
        },
        check_cmd="python3 check.py",
        tags=["bugfix", "navigation"],
    ),
]


def get_task(task_id: str) -> Task | None:
    return next((t for t in ALL_TASKS if t.id == task_id), None)


def select(ids=None, tags=None) -> list:
    """按 id 或 tag 选任务。都不给则返回全部。"""
    out = ALL_TASKS
    if ids:
        want = set(ids)
        out = [t for t in out if t.id in want]
    if tags:
        want = set(tags)
        out = [t for t in out if want & set(t.tags)]
    return out
