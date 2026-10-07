from tools import (
    create_plan,
    get_plan,
    is_safe_command,
    update_plan,
)


class TestPlan:
    def test_create_plan(self):
        result = create_plan(["步骤1", "步骤2", "步骤3"])
        assert "已创建计划" in result
        assert "步骤1" in result
        assert "步骤2" in result
        assert "步骤3" in result

    def test_update_plan_done(self):
        create_plan(["步骤1", "步骤2"])
        result = update_plan(1, "done")
        assert "✓" in result

    def test_update_plan_failed(self):
        create_plan(["步骤1"])
        result = update_plan(1, "failed")
        assert "✗" in result

    def test_get_plan(self):
        create_plan(["测试步骤"])
        result = get_plan()
        assert "测试步骤" in result


class TestIsSafeCommand:
    def test_safe_commands(self):
        assert is_safe_command("ls -la") is True
        assert is_safe_command("cat file.txt") is True
        assert is_safe_command("pwd") is True
        assert is_safe_command("git status") is True
        assert is_safe_command("python test.py") is True

    def test_dangerous_commands(self):
        assert is_safe_command("rm -rf /") is False
        assert is_safe_command("sudo apt install") is False
        assert is_safe_command("mkfs.ext4 /dev/sda") is False
        assert is_safe_command("dd if=/dev/zero of=/dev/sda") is False

    def test_pipe_and_redirect(self):
        assert is_safe_command("ls | rm") is False
        assert is_safe_command("ls && rm -rf /") is False
        assert is_safe_command("echo test > /etc/passwd") is False
        assert is_safe_command("cat file < script.sh") is False

    def test_python_dash_c(self):
        # python -c 危险（可执行任意代码）
        assert is_safe_command("python -c 'print(1)'") is False
        # python xxx.py 安全
        assert is_safe_command("python test.py") is True
