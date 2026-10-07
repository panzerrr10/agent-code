import pytest
import db


@pytest.fixture(autouse=True)
def setup_db():
    """每个测试前初始化数据库"""
    db.init_db()
    yield


class TestSession:
    def test_create_session(self):
        sid = db.create_session("测试会话")
        assert sid > 0
        # 清理
        db.delete_session(sid)

    def test_session_lifecycle(self):
        # 创建
        sid = db.create_session("测试会话")
        assert sid > 0

        # 保存消息
        db.save_message(sid, "user", "你好")
        db.save_message(sid, "assistant", "你好！")

        # 读消息
        msgs = db.get_messages(sid)
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "你好"
        assert msgs[1]["role"] == "assistant"

        # 删会话
        db.delete_session(sid)
        assert len(db.get_messages(sid)) == 0


class TestMemory:
    def test_memory_lifecycle(self):
        # 存
        mid = db.save_memory("测试记忆")
        assert mid > 0

        # 查
        memories = db.get_all_memories()
        assert any(m["content"] == "测试记忆" for m in memories)

        # 删
        db.delete_memory(mid)
        memories = db.get_all_memories()
        assert not any(m["content"] == "测试记忆" for m in memories)