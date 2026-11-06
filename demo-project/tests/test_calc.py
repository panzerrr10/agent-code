import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.calc import add, average  # noqa: E402


def test_add():
    assert add(2, 3) == 5


def test_average():
    assert average([1, 2, 3]) == 2
