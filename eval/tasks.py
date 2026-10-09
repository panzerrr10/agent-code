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



# ============ 难题：用来提高区分度 ============
#
# 前面 10 条都是 5~8 步就能做完的单点任务，模型全过 —— 满分等于没有区分度。
# 这一批刻意加难，目标是：
#   1. 步数明显更高（需要探索、需要多轮验证）
#   2. 有「看起来对但边界错」的陷阱
#   3. 至少一条能**触发上下文压缩**（现在 10 条全是 compactions=0，压缩路径根本没被覆盖）


def _big_module(name: str, n_funcs: int, bug_at: int, bug_kind: str) -> str:
    """生成一个大模块（几百行），其中某一个函数有 bug。

    为什么用生成而不是手写：几百行的字面量会让 tasks.py 没法读，
    而生成出来的结构和真实代码一样（大量相似函数 = 真正的「大海捞针」）。
    """
    parts = [f'"""自动生成的模块 {name}：一组相似的工具函数。"""', "", "import math", ""]
    for i in range(n_funcs):
        parts.append(f"def op_{name}_{i}(x, y=1):")
        parts.append(f'    """第 {i} 号运算：把 x 和 y 做一些变换。"""')
        if i == bug_at:
            if bug_kind == "sign":
                parts.append("    return abs(x) - abs(y)      # 这里应该是加")
            elif bug_kind == "offbyone":
                parts.append("    return x * (y + 1)          # 这里不应该 +1")
            else:
                parts.append("    return x / y if y else 0    # 这里应该返回 x")
        else:
            parts.append("    return abs(x) + abs(y)")
        # 掺一些无害的注释，让文件更接近真实体量
        parts.append(f"    # 备注：op_{name}_{i} 在 v1 中行为不变")
        parts.append("")
    return "\n".join(parts) + "\n"


# 大海捞针：6 个文件、每个约 90 个函数，合计几千行。
# 必须真的读进去才能定位（bug 是纯逻辑错误，grep 关键词找不到）。
_HAYSTACK_BUG_FILE = "mod_d.py"      # 注意要带 .py —— 少写一次就会一个 bug 都植入不进去
_HAYSTACK_BUG_AT = 37
_HAYSTACK_FILES = {
    f"mod_{c}.py": _big_module(c, 90, _HAYSTACK_BUG_AT if f"mod_{c}.py" == _HAYSTACK_BUG_FILE else -1,
                               "sign")
    for c in "abcdef"
}
_HAYSTACK_FILES["check.py"] = (
    "import importlib\n"
    "\n"
    "bad = 0\n"
    "for c in 'abcdef':\n"
    "    m = importlib.import_module(f'mod_{c}')\n"
    "    for i in range(90):\n"
    "        if getattr(m, f'op_{c}_{i}')(-3, 2) != 5:\n"
    "            bad += 1\n"
    "assert bad == 0, f'有 {bad} 个函数的计算结果不对（自己找出来是哪几个）'\n"
    'print("ok")\n'
)


HARD_TASKS = [
    Task(
        id="spec_edge_cases",
        title="按规格实现（含多个边界）",
        prompt=(
            "duration.py 里的 parse_duration 还没实现。"
            "请严格按文档字符串里的规格实现，让 check.py 通过。"
        ),
        files={
            "duration.py": (
                "def parse_duration(text):\n"
                '    """把形如 \'1h30m\' / \'45s\' / \'2d\' 的时长解析成秒数。\n'
                "\n"
                "    规格：\n"
                "      - 单位有 s / m / h / d，分别表示 秒/分/时/天\n"
                "      - 可以任意顺序、任意组合、可重复（'1m1m' == 120）\n"
                "      - 允许数字与单位之间有空格（'1 h 30 m'）\n"
                "      - 数字可以是多位（'90m' == 5400）\n"
                "      - **纯数字（没有单位）视为秒**（'90' == 90）\n"
                "      - 空字符串 / 全是空白 → 抛 ValueError\n"
                "      - 出现未知单位、或数字后面缺单位但又不是纯数字 → 抛 ValueError\n"
                "      - **负数 → 抛 ValueError**\n"
                '    """\n'
                "    raise NotImplementedError\n"
            ),
            "check.py": (
                "from duration import parse_duration\n"
                "\n"
                "cases = {\n"
                "    '45s': 45, '90': 90, '1m': 60, '1h': 3600, '2d': 172800,\n"
                "    '1h30m': 5400, '1m1m': 120, '1 h 30 m': 5400, '90m': 5400,\n"
                "    '1d2h3m4s': 86400 + 7200 + 180 + 4, '  10s  ': 10,\n"
                "}\n"
                "for text, want in cases.items():\n"
                "    got = parse_duration(text)\n"
                "    assert got == want, f'{text!r} -> {got}，应为 {want}'\n"
                "\n"
                "for bad in ['', '   ', 'abc', '1x', '1h-2m', '-5', '-1h', 'h', '1h 2']:\n"
                "    try:\n"
                "        parse_duration(bad)\n"
                "    except ValueError:\n"
                "        continue\n"
                "    raise AssertionError(f'{bad!r} 应当抛 ValueError')\n"
                'print("ok")\n'
            ),
        },
        check_cmd="python3 check.py",
        tags=["hard", "spec-precision", "edge-cases"],
    ),

    Task(
        id="cascade_root_cause",
        title="找出根因而不是逐个打补丁",
        prompt=(
            "check.py 报了多处不一致。这些失败很可能来自同一个根因，"
            "请找到并修复**根因**，而不是在每个调用点分别打补丁。"
        ),
        files={
            "shapes.py": (
                "def area(kind, a, b=None):\n"
                '    """算面积。kind: rect / tri / circle。"""\n'
                "    if kind == 'rect':\n"
                "        return a * b\n"
                "    if kind == 'tri':\n"
                "        return a * b / 2\n"
                "    if kind == 'circle':\n"
                "        return 3.14 * a * a\n"
                "    raise ValueError(kind)\n"
            ),
            "r1.py": "from shapes import area\n\n\ndef f1(): return area('rect', 3, 4)\n",
            "r2.py": "from shapes import area\n\n\ndef f2(): return area('tri', 4, 6)\n",
            "r3.py": "from shapes import area\n\n\ndef f3(): return area('circle', 2)\n",
            "r4.py": "from shapes import area\n\n\ndef f4(): return area('rect', 5, 5)\n",
            "r5.py": "from shapes import area\n\n\ndef f5(): return area('tri', 2, 2)\n",
            "check.py": (
                "from r1 import f1\n"
                "from r2 import f2\n"
                "from r3 import f3\n"
                "from r4 import f4\n"
                "from r5 import f5\n"
                "\n"
                "assert f1() == 12, f1()\n"
                "assert f2() == 12, f2()\n"
                "assert f3() == 12, f3()\n"
                "assert f4() == 25, f4()\n"
                "assert f5() == 2, f5()\n"
                'print("ok")\n'
            ),
        },
        check_cmd="python3 check.py",
        # 根因修好 = 所有调用点都不用改；用 git 检查 r*.py 没被动过
        extra_check="git diff --quiet -- r1.py r2.py r3.py r4.py r5.py",
        tags=["hard", "root-cause"],
    ),

    Task(
        id="needle_in_haystack",
        title="在几千行里定位一个逻辑错误",
        prompt=(
            "项目里有 6 个结构相似的模块，每个有 90 个函数。"
            "其中**恰好一个**函数的逻辑写错了，check.py 会告诉你哪里不对。"
            "请找出并修复它。"
        ),
        files=dict(_HAYSTACK_FILES),
        check_cmd="python3 check.py",
        timeout=900,
        tags=["hard", "navigation", "context-pressure"],
    ),

    Task(
        id="fix_and_add_test",
        title="修 bug 并补一个回归测试",
        prompt=(
            "percent.py 里的 pct_change 在「基数为 0」时行为不对。"
            "请修复它，**并补一个能覆盖这个场景的回归测试**（写进 tests.py）。"
        ),
        files={
            "percent.py": (
                "def pct_change(old, new):\n"
                '    """变化百分比。old 为 0 时无法计算，应返回 None。"""\n'
                "    return (new - old) / old * 100      # 没处理 old == 0\n"
            ),
            "tests.py": (
                "from percent import pct_change\n"
                "\n"
                "assert pct_change(100, 150) == 50.0\n"
                'print("ok")\n'
            ),
            "check.py": (
                "from percent import pct_change\n"
                "\n"
                "assert pct_change(100, 150) == 50.0\n"
                "assert pct_change(100, 50) == -50.0\n"
                "assert pct_change(0, 10) is None, pct_change(0, 10)\n"
                'print("ok")\n'
            ),
        },
        check_cmd="python3 check.py",
        # 必须真的补了测试，而且这个测试能抓住原 bug
        extra_check=(
            "python3 -c \"import subprocess,sys;"
            "src=open('tests.py').read();"
            "sys.exit(0 if 'pct_change(0' in src.replace(' ','') else 1)\""
        ),
        tags=["hard", "test-writing"],
    ),

    Task(
        id="multi_step_migration",
        title="多步迁移：改名 + 补功能 + 更新所有调用方",
        prompt=(
            "把 store.py 里的 get 改名为 fetch，给它加上默认值参数 default，"
            "并更新所有调用方。check.py 必须通过，且不能留下旧的 get。"
        ),
        files={
            "store.py": (
                "class Store:\n"
                "    def __init__(self):\n"
                "        self._d = {}\n"
                "\n"
                "    def put(self, k, v):\n"
                "        self._d[k] = v\n"
                "\n"
                "    def get(self, k):\n"
                "        return self._d[k]\n"
            ),
            "svc_a.py": "from store import Store\n\n\ndef a(s):\n    return s.get('a')\n",
            "svc_b.py": "from store import Store\n\n\ndef b(s):\n    return s.get('b')\n",
            "check.py": (
                "from store import Store\n"
                "from svc_a import a\n"
                "from svc_b import b\n"
                "\n"
                "s = Store()\n"
                "s.put('a', 1)\n"
                "s.put('b', 2)\n"
                "assert s.fetch('a') == 1\n"
                "assert s.fetch('zzz', 'fallback') == 'fallback'\n"
                "assert a(s) == 1 and b(s) == 2\n"
                "assert not hasattr(s, 'get'), '旧的 get 应当已改名'\n"
                'print("ok")\n'
            ),
        },
        check_cmd="python3 check.py",
        # 用 AST 判断「store.py 里没有名为 get 的方法」。
        # 早先用的是 grep '\\.get(' —— 会把实现内部正当使用的 dict.get() 也匹配上，
        # 造成「模型明明做对了却判失败」。调用方是否更新由 check.py 的 a(s)/b(s) 覆盖。
        extra_check=(
            "python3 -c \"import ast,sys;"
            "t=ast.parse(open('store.py').read());"
            "n=[x.name for x in ast.walk(t) if isinstance(x, ast.FunctionDef)];"
            "sys.exit(1 if 'get' in n else 0)\""
        ),
        tags=["hard", "migration", "multi-file"],
    ),

    Task(
        id="strict_output_format",
        title="生成严格格式的报告",
        prompt=(
            "report.py 的 build 还没实现。请按文档字符串里的格式要求实现，"
            "输出必须逐字符匹配（check.py 会比对）。"
        ),
        files={
            "report.py": (
                "def build(rows):\n"
                '    """生成定宽文本表格。\n'
                "\n"
                "    规格（必须逐字符一致）：\n"
                "      - 每行 `| 值 |`，值左对齐，列宽 = 该列所有值中最长者的长度\n"
                "      - 第一行是表头，表头下面是一条分隔线：每列 `+` 后跟 (宽度+2) 个 `-`\n"
                "      - 分隔线形如 `+-----+----+`（首尾都是 +）\n"
                "      - 值里若有非 ASCII 字符，**按显示宽度算**（中文算 2，其它算 1）\n"
                "      - 空 rows → 返回空字符串\n"
                "      - 每行以 \\\\n 结尾（最后一行也有）\n"
                '    """\n'
                "    raise NotImplementedError\n"
            ),
            "check.py": (
                "from report import build\n"
                "\n"
                "got = build([['name', 'n'], ['ab', '1'], ['c', '22']])\n"
                "want = (\n"
                "    '| name | n  |\\n'\n"
                "    '+------+----+\\n'\n"
                "    '| ab   | 1  |\\n'\n"
                "    '| c    | 22 |\\n'\n"
                ")\n"
                "assert got == want, repr(got)\n"
                "assert build([]) == ''\n"
                "\n"
                "# 非 ASCII 按显示宽度算\n"
                "g2 = build([['k', 'v'], ['中文', 'x']])\n"
                "w2 = '| k    | v |\\n+------+---+\\n| 中文 | x |\\n'\n"
                "assert g2 == w2, repr(g2)\n"
                'print("ok")\n'
            ),
        },
        check_cmd="python3 check.py",
        tags=["hard", "format-strict"],
    ),
]


# 合并进总表：难度递增，难题放后面
ALL_TASKS.extend(HARD_TASKS)

# 这条是**唯一**可能累积到触发上下文压缩的任务。
# 别的任务 agent 会用 search_files / run_python 抄近路，上下文根本涨不起来 ——
# 12 个文件要逐个读改验证，每一步的工具结果都留在 messages 里。
ALL_TASKS.append(Task(
    id="many_small_fixes",
    title="连续修 12 个独立的小 bug",
    prompt="check.py 会列出所有问题，请逐个修复，让 check.py 全部通过。",
    files={
        **{f"u{i:02d}.py": f"def v():\n    return {i} + 1   # 应为 {i}\n" for i in range(1, 13)},
        "check.py": (
            "import importlib\n"
            "\n"
            "bad = []\n"
            "for i in range(1, 13):\n"
            "    m = importlib.import_module(f'u{i:02d}')\n"
            "    if m.v() != i:\n"
            "        bad.append(f'u{i:02d}.v() == {m.v()}，应为 {i}')\n"
            "assert not bad, bad\n"
            'print("ok")\n'
        ),
    },
    check_cmd="python3 check.py",
    timeout=900,
    tags=["hard", "persistence", "context-pressure"],
))


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
