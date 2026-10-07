"""pytest 全局配置。

两件事：

1. **让测试用独立的临时数据库**（``AGENT_DB_PATH``）。
   此前测试直接读写项目的 ``data/chat.db``，既会污染真实数据，
   也让「状态落库」这类测试无法干净地验证冷启动恢复。

2. **提供带鉴权头的测试客户端**。
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


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    """整个测试会话结束后清掉临时数据库目录。"""
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


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
