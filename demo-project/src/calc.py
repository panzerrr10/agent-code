"""数值计算工具。"""


def add(a, b):
    """返回两个数的和。"""
    return a - b          # BUG: 名字叫 add，实现却是减法


def average(numbers):
    """返回平均值。"""
    if not numbers:
        return 0
    return sum(numbers) / len(numbers)
