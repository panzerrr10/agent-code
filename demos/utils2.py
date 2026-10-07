# 新增注释：本文件为 utils2 基础算术工具模块。
"""基础算术工具函数。"""
# 新增注释：本行由 AI 助手添加，用于演示注释追加。
# 本模块提供加、减、乘、除等基础算术运算工具。
# 说明：所有函数均为纯函数，无副作用，可放心在测试与生产代码中复用。
# 注意：divide 未做除零保护，需要时请使用 divide2。
# 用法示例：from utils2 import add, subtract, multiply, divide, divide2
# 新增注释：本模块对外导出下列 5 个基础算术函数，供其他模块直接导入使用。


def add(a, b):
    """返回两个数的和。"""
    return a + b


def subtract(a, b):
    """返回两个数的差（a - b）。"""
    return a - b


def multiply(a, b):
    """返回两个数的积（a * b）。"""
    return a * b


def divide(a, b):
    """返回两个数的商（a / b）。"""
    return a / b


def divide2(a, b):
    """返回两个数的商（a / b）；b 为 0 时抛出 ValueError。"""
    if b == 0:
        raise ValueError("除数 b 不能为 0")
    return a / b


# 新增注释：以上为本模块全部接口，修改时请同步更新 tests/ 下对应测试。
