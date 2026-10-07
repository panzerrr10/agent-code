"""
AI Agent 桌面应用启动器
用 pywebview 把 Flask 应用包装成桌面窗口
"""

import subprocess
import threading
import time

import webview

from app import app
from paths import WEBVIEW_DATA_DIR


def ensure_sandbox():
    """确保 Docker 沙箱容器在运行"""
    try:
        result = subprocess.run(
            ["docker", "ps", "-q", "-f", "name=agent-sandbox"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.stdout.strip():
            print("✅ Docker 沙箱已在运行")
            return

        result = subprocess.run(
            ["docker", "ps", "-aq", "-f", "name=agent-sandbox"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.stdout.strip():
            subprocess.run(["docker", "start", "agent-sandbox"], timeout=10)
            print("✅ 已启动 Docker 沙箱")
        else:
            print("⚠️ Docker 沙箱不存在，请先创建容器")
    except FileNotFoundError:
        print("⚠️ 未检测到 Docker，执行类工具将不可用")
    except Exception as e:
        print(f"⚠️ 无法检查 Docker 沙箱：{e}")


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
