import pytest
from utils import call_llm_with_retry


class MockClient:
    """模拟 LLM 客户端"""

    def __init__(self, fail_times=0):
        self.fail_times = fail_times
        self.call_count = 0

    class _Completions:
        def __init__(self, parent):
            self.parent = parent

        def create(self, **kwargs):
            self.parent.call_count += 1
            if self.parent.call_count <= self.parent.fail_times:
                raise Exception("模拟网络错误")
            return {"choices": [{"message": {"content": "OK"}}]}

    @property
    def chat(self):
        class _Chat:
            def __init__(self, parent):
                self.completions = MockClient._Completions(parent)

        return _Chat(self)


class TestRetry:
    def test_success_first_try(self):
        client = MockClient(fail_times=0)
        result = call_llm_with_retry(client, model="test", messages=[])
        assert result["choices"][0]["message"]["content"] == "OK"
        assert client.call_count == 1

    def test_retry_then_success(self):
        client = MockClient(fail_times=2)
        result = call_llm_with_retry(client, model="test", messages=[])
        assert result["choices"][0]["message"]["content"] == "OK"
        assert client.call_count == 3  # 2 次失败 + 1 次成功