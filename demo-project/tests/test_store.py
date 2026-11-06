import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.store import add_task, complete, list_tasks  # noqa: E402


def test_add_and_list():
    add_task("写演示脚本")
    assert len(list_tasks()) >= 1


def test_complete():
    i = add_task("验证完成状态")
    complete(i)
    assert list_tasks()[i]["done"] is True
