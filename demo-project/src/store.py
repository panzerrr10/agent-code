"""任务存取（内存版）。"""

_tasks = []


def add_task(title):
    _tasks.append({"title": title, "done": False})
    return len(_tasks) - 1


def complete(index):
    _tasks[index]["done"] = True


def list_tasks():
    return list(_tasks)
