"""pytest 全局配置。

三件事：

1. **让测试用独立的临时数据库**（``AGENT_DB_PATH``）。
   此前测试直接读写项目的 ``data/chat.db``，既会污染真实数据，
   也让「状态落库」这类测试无法干净地验证冷启动恢复。

2. **让测试用独立的临时回收站**（自动生效，见 ``_isolate_trash``）。
   此前只有部分用例手动重定向 ``trash.TRASH_DIR``，漏掉的那些会把备份
   写进真实的 ``data/.trash``——曾经的后果是用户的回收站里堆了 100 多条
   ``multiedit_f1.txt`` 之类的测试垃圾。

3. **提供带鉴权头的测试客户端**。
   服务端已强制要求 ``X-Auth-Token``，这里统一注入，避免每个用例各写一遍；
   另提供 ``anon_client`` 专门用于验证「没令牌会被拒绝」。
"""

import os
import shutil
import tempfile

# 必须在导入项目模块之前设置：paths 在 import 时就固定了 DB_PATH
_TMP_DIR = tempfile.mkdtemp(prefix="agentcode-test-")
os.environ.setdefault("AGENT_DB_PATH", os.path.join(_TMP_DIR, "chat.db"))

import pytest  # noqa: E402

import app as app_module  # noqa: E402
import auth  # noqa: E402
import trash  # noqa: E402


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    """整个测试会话结束后清掉临时数据库目录。"""
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


@pytest.fixture(autouse=True)
def _isolate_trash(tmp_path, monkeypatch):
    """所有用例一律不得写入真实回收站。

    做成 autouse 是为了「不可能漏」：写文件类用例即使忘了自己重定向，
    备份也只会落在 pytest 的临时目录里。个别用例若需要自己的回收站路径，
    仍可再 monkeypatch 覆盖。
    """
    d = tmp_path / "trash"
    d.mkdir(exist_ok=True)
    monkeypatch.setattr(trash, "TRASH_DIR", str(d))
    yield


@pytest.fixture
def client():
    """带鉴权头的 Flask 测试客户端（大多数接口用例都用它）。"""
    c = app_module.app.test_client()
    c.environ_base["HTTP_X_AUTH_TOKEN"] = auth.get_token()
    return c


@pytest.fixture
def anon_client():
    """不带鉴权头的客户端，用于验证 401。"""
    return app_module.app.test_client()
