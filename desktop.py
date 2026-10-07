"""
AI Agent 桌面应用启动器
用 pywebview 把 Flask 应用包装成桌面窗口
"""

import threading
import time

import webview

from app import app
from paths import WEBVIEW_DATA_DIR


def ensure_sandbox():
    """检查 Docker 沙箱是否就绪。

    现在每次执行都用 `docker run --rm` 起临时容器，并把**当前项目根目录**
    挂载成 /workspace，因此不再需要常驻的 agent-sandbox 容器，只需镜像存在。
    """
    import sandbox

    if not sandbox.docker_available():
        print("⚠️ 未检测到可用的 Docker：run_python / run_command 将被拒绝执行")
        print("   如需执行代码，请先安装并启动 Docker")
        return
    if not sandbox.image_available():
        print(f"⚠️ 找不到沙箱镜像 `{sandbox.IMAGE}`，执行类工具不可用")
        print(f"   请先构建：docker build -t {sandbox.IMAGE} .")
        return
    print(
        f"✅ Docker 沙箱就绪（镜像 {sandbox.IMAGE}，network={sandbox.NETWORK}，"
        f"memory={sandbox.MEMORY}，cpus={sandbox.CPUS}）"
    )


def start_flask():
    """在后台线程里启动 Flask"""
    app.run(
        host="127.0.0.1",
        port=5004,
        debug=False,
        use_reloader=False,
    )


def main():
    import sys
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("com.pz.aiagent.desktop")
        except Exception:
            pass

    print("🚀 启动 AI Agent 桌面应用...")

    # 1. 确保 Docker 沙箱
    ensure_sandbox()

    # 2. 后台启动 Flask
    flask_thread = threading.Thread(target=start_flask, daemon=True)
    flask_thread.start()

    # 3. 等 Flask 起来
    time.sleep(1.5)

    # 4. 创建 pywebview 窗口
    webview.create_window(
        title="AI Agent",
        url="http://127.0.0.1:5004",
        width=1200,
        height=800,
        min_size=(800, 600),
        text_select=True,
    )

    # 5. 启动窗口
    webview.start(
        private_mode=False,
        storage_path=WEBVIEW_DATA_DIR,
    )

    print("👋 AI Agent 已关闭")


if __name__ == "__main__":
    main()
