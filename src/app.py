import json
import logging
import os
import threading
import time

from flask import Flask, Response, jsonify, render_template, request, send_file

import auth
import db
import paths
import sandbox
from agent import run_agent_stream
from config_providers import DEFAULT_PROVIDER, RAG_PROVIDER, get_provider, get_provider_list
from paths import ICON_PATH, STATIC_DIR, TEMPLATES_DIR, is_path_permitted

logger = logging.getLogger(__name__)

# 前端资源在 <root>/web/{templates,static}，必须显式告诉 Flask，
# 否则它会去 src/ 下找 templates/static，页面与静态资源全 404
app = Flask(
    __name__,
    template_folder=TEMPLATES_DIR,
    static_folder=STATIC_DIR,
)
db.init_db()

# MCP 插件：后台连接已启用的 server（**不阻塞启动** —— 慢 server 不该拖死应用）
def _mcp_boot():
    try:
        import mcp_client
        mcp_client.start_background_connect()
    except Exception:
        logger.warning("MCP 后台连接启动失败", exc_info=True)


threading.Thread(target=_mcp_boot, name="mcp-app-boot", daemon=True).start()


@app.before_request
def _enforce_auth():
    """最小鉴权：所有接口都要求 X-Auth-Token（页面外壳与静态资源除外）。

    跨域页面无法设置自定义请求头、也过不了预检，因此 CSRF 路径被切断。
    """
    if request.method == "OPTIONS":
        return None
    if auth.is_public_path(request.path):
        return None
    if auth.verify(request.headers.get(auth.HEADER_NAME)):
        return None
    return (
        jsonify({"error": "未授权：缺少或无效的 X-Auth-Token", "code": "unauthorized"}),
        401,
    )


def _pack_user_content(task: str, images=None) -> str:
    """把用户消息打包成可写入 SQLite 的字符串。

    - 有图片：存 JSON（{"text": ..., "images": [data URL, ...]}），
      图片以 Base64 data URL 内联，刷新页面 / 切换会话后仍能从历史回显。
    - 无图片：保持纯文本，兼容历史旧数据与导出、上下文裁剪逻辑。
    """
    if images:
        return json.dumps({"text": task, "images": images}, ensure_ascii=False)
    return task


def _sse_error(message: str) -> str:
    """构造一条 SSE 错误事件。"""
    return (
        "data: "
        + json.dumps({"type": "error", "content": message}, ensure_ascii=False)
        + "\n\n"
    )


def _build_evidence(tool_events) -> str:
    """把 Executor 的工具调用记录压成给 Reviewer 的证据摘要。

    只保留工具名与结果片段，避免把整份文件内容塞进审查请求。
    """
    if not tool_events:
        return "（本次执行未调用任何工具）"
    lines = []
    for ev in tool_events:
        if ev.get("type") == "tool_call":
            args = ev.get("args") or {}
            brief = ", ".join(
                f"{k}={str(v)[:120]}" for k, v in list(args.items())[:5]
            )
            lines.append(f"- 调用 {ev.get('name')}({brief})")
        elif ev.get("type") == "tool_result":
            content = str(ev.get("content") or "").replace("\n", " ")[:300]
            lines.append(f"  结果：{content}")
    return "\n".join(lines)


@app.route("/favicon.ico")
def favicon():
    return send_file(ICON_PATH, mimetype="image/png")


def _pick_folder(title: str, initial: str | None):
    """弹系统「选择文件夹」对话框，返回绝对路径；取消返回 None。

    优先用 **zenity**（Ubuntu 24 上是 4.x，即 GTK4 实现）——观感与系统其它程序
    （比如浏览器上传文件时）完全一致：左侧「最近使用 / 主目录 / 视频 / 图片…」
    侧栏、橙色确认按钮。

    原先用 tkinter 的 ``askdirectory``，弹出来是 Tk 自带的老式对话框，与系统不一致。
    tkinter 仍保留作兜底（zenity 缺失或没有图形环境时）。
    """
    import shutil
    import subprocess

    if shutil.which("zenity"):
        cmd = ["zenity", "--file-selection", "--directory", f"--title={title}"]
        if initial and os.path.isdir(initial):
            # 末尾的 / 让 zenity 直接定位到该目录
            cmd.append(f"--filename={initial.rstrip('/')}/")
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            # 用户取消时 zenity 退出码为 1、无输出，不当作错误
            return (r.stdout or "").strip() or None
        except subprocess.TimeoutExpired:
            return None
        except Exception:
            logger.warning("zenity 弹窗失败，回退 tkinter", exc_info=True)

    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        folder = filedialog.askdirectory(title=title, mustexist=True)
        root.destroy()
        return folder or None
    except Exception:
        logger.warning("tkinter 弹窗也失败", exc_info=True)
        return None


@app.route("/api/select_folder", methods=["POST"])
def select_folder():
    """弹系统原生「选择文件夹」对话框，返回绝对路径。

    对调用方保持兼容（仍返回 ok/path），另加 cancelled 以便前端区分
    「用户取消」与「弹窗失败」——取消不该弹错误提示。
    """
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "选择文件夹").strip()
    initial = (data.get("initial") or "").strip() or None

    try:
        path = _pick_folder(title, initial)
    except Exception as e:  # pragma: no cover - 依赖本机图形环境
        return jsonify({"ok": False, "error": f"弹窗失败：{e}"}), 500

    if not path:
        return jsonify({"ok": False, "cancelled": True})
    return jsonify({"ok": True, "path": path})


@app.route("/api/project_tree", methods=["GET"])
def project_tree():
    """返回指定目录的树形结构 JSON。
    参数：
      - path: 目录路径（默认用 current_project_root）
      - depth: 最大深度（默认 3）
    """
    import os

    import runtime

    path = request.args.get("path") or runtime.get_project_root()
    depth = int(request.args.get("depth", 3))

    if not is_path_permitted(path):
        return jsonify({"error": f"拒绝访问：{path} 不在允许范围内"}), 403

    if not os.path.isdir(path):
        return jsonify({"error": f"不是目录：{path}"}), 400

    # 跳过这些目录
    SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv",
                 "chroma_db", ".pytest_cache", ".mypy_cache", ".trash",
                 ".backup", ".webview_data", "data", ".idea", ".vscode"}

    def _walk(p, level):
        if level > depth:
            return []
        items = []
        try:
            entries = sorted(os.listdir(p), key=lambda x: (not os.path.isdir(os.path.join(p, x)), x.lower()))
        except PermissionError:
            return []
        for name in entries:
            full = os.path.join(p, name)
            if os.path.isdir(full):
                if name in SKIP_DIRS:
                    continue
                items.append({
                    "name": name,
                    "type": "dir",
                    "path": full,
                    "children": _walk(full, level + 1) if level < depth else [],
                })
            else:
                try:
                    size = os.path.getsize(full)
                except Exception:
                    size = 0
                items.append({
                    "name": name,
                    "type": "file",
                    "path": full,
                    "size": size,
                })
        return items

    tree = {
        "name": os.path.basename(path) or path,
        "type": "dir",
        "path": path,
        "children": _walk(path, 1),
    }
    return jsonify({"ok": True, "tree": tree})


# ============ 附件（上传给 Agent 看 / 改）与下载 ============
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
# 这些后缀直接按 UTF-8 文本读；.pdf / .docx 交给 rag 里的解析器
TEXT_EXT = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".xml", ".html", ".htm", ".css", ".scss", ".less",
    ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".py", ".rb", ".go", ".rs", ".java",
    ".kt", ".c", ".h", ".cpp", ".hpp", ".cs", ".php", ".sh", ".bash", ".zsh", ".sql",
    ".env", ".gitignore", ".dockerfile",
}


def _extract_attachment_text(name: str, raw: bytes) -> tuple[str | None, str | None]:
    """把上传的附件转成文本。返回 (text, error)。"""
    ext = os.path.splitext(name)[1].lower()
    if ext in (".pdf", ".docx"):
        try:
            import rag

            return rag.extract_text(name, raw), None
        except Exception as e:
            return None, f"解析失败：{e}"
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "不是 UTF-8 文本（二进制或其它编码），无法作为文档附件"


@app.route("/api/attach", methods=["POST"])
def api_attach():
    """接收附件并落盘，返回可供 Agent 使用的信息。

    落盘位置：``~/.agent-code/uploads/<会话>/``。放在 home 内是为了让 Agent 的
    read_file / edit_file 一定能操作它（工具白名单只放行 home），
    这样「按需求改这份文档」就是一次普通的 edit_file。
    """
    import paths as _paths

    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "没有收到文件"}), 400

    raw = f.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        return jsonify(
            {"ok": False, "error": f"文件太大（{len(raw) / 1024 / 1024:.1f} MB），上限 10 MB"}
        ), 400

    name = os.path.basename(f.filename) or "attachment"
    text, err = _extract_attachment_text(name, raw)
    if err:
        return jsonify({"ok": False, "error": err}), 400

    sid = str(request.form.get("session_id") or "shared")
    safe_sid = "".join(c for c in sid if c.isalnum() or c in "-_") or "shared"
    stamp = time.strftime("%Y%m%d_%H%M%S")
    safe_name = "".join(c for c in name if c.isalnum() or c in "._- ").strip() or "attachment"
    folder = os.path.join(_paths.UPLOAD_DIR, safe_sid)
    os.makedirs(folder, exist_ok=True)
    dest = os.path.join(folder, f"{stamp}_{safe_name}")
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write(text)

    return jsonify(
        {
            "ok": True,
            "name": name,
            "path": dest,
            "size": len(raw),
            "chars": len(text),
            "preview": text[:400],
        }
    )


def _load_attachments(items) -> list:
    """把前端传来的 [{path, name}] 读成带正文的附件（供拼进消息）。"""
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        path = (it.get("path") or "").strip()
        if not path or not is_path_permitted(path) or not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except Exception:
            continue
        out.append(
            {"name": it.get("name") or os.path.basename(path), "path": path, "text": text}
        )
    return out


@app.route("/api/download", methods=["GET"])
def download_file():
    """下载文件（浏览器默认存到系统「下载」目录）。

    实测本机 Chromium 会遵循 XDG 配置存到 ``/home/pz/下载``，所以这里只要
    带上 Content-Disposition 即可，不需要服务端写盘（也就没有越权写文件的风险）。
    """
    path = request.args.get("path", "")
    if not path:
        return jsonify({"ok": False, "error": "缺少 path 参数"}), 400
    if not is_path_permitted(path):
        return jsonify({"ok": False, "error": f"拒绝下载：{path} 不在允许范围内"}), 403
    if not os.path.isfile(path):
        return jsonify({"ok": False, "error": f"文件不存在：{path}"}), 400
    # 允许前端指定下载显示名（附件落盘时带了时间戳前缀，直接下会很难看）
    wanted = os.path.basename((request.args.get("name") or "").strip())
    download_name = wanted or os.path.basename(path)
    return send_file(path, as_attachment=True, download_name=download_name)


# ============ 文件读写（右侧文件面板）============
# 编辑上限。超过就完全不返回内容，而不是截断——**截断的内容一旦被保存，
# 文件尾部就被删掉了**，所以宁可不让编辑。
MAX_EDIT_BYTES = 1_000_000


def _sha256_file(path: str) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_for_editor(path: str) -> tuple[dict, int]:
    """读取文件供编辑器使用。返回 (payload, http_status)。

    三条底线：
    1. **非 UTF-8 一律不给编辑**。以前这里是 ``errors="ignore"``，会静默丢掉
       解不开的字节；「读出来→改→存回去」会把 GBK 等文件永久写坏。
    2. **二进制不给编辑**（首 8KB 出现 NUL 字节即判定）。
    3. **超限不给编辑**，绝不返回截断内容。
    """
    size = os.path.getsize(path)
    mtime = os.path.getmtime(path)
    base = {
        "ok": True,
        "path": path,
        "size": size,
        "mtime": mtime,
        "hash": _sha256_file(path),
    }

    if size > MAX_EDIT_BYTES:
        base.update(
            editable=False,
            reason="too-large",
            message=f"文件 {size / 1024:.0f} KB，超过 {MAX_EDIT_BYTES // 1000} KB 的编辑上限，未加载内容",
        )
        return base, 200

    try:
        with open(path, "rb") as f:
            raw = f.read()
    except Exception as e:
        return {"ok": False, "error": f"读取失败：{e}"}, 500

    if b"\x00" in raw[:8192]:
        base.update(editable=False, reason="binary", message="二进制文件，无法以文本方式编辑")
        return base, 200

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        base.update(
            editable=False,
            reason="not-utf8",
            message=f"不是 UTF-8 编码（第 {e.start} 字节处解码失败），为避免写坏文件已禁止编辑",
        )
        return base, 200

    # 换行符统一成 LF 交给编辑器，保存时再还原，保证「没改就不动文件」
    eol = "crlf" if "\r\n" in text else "lf"
    if eol == "crlf":
        text = text.replace("\r\n", "\n")

    base.update(editable=True, content=text, eol=eol, encoding="utf-8")
    return base, 200


def _atomic_write_text(path: str, text: str) -> None:
    """原子写：先写同目录临时文件再 os.replace，避免写一半崩溃留下半个文件。"""
    import tempfile

    directory = os.path.dirname(path) or "."
    mode = os.stat(path).st_mode
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".agentcode-tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.chmod(tmp, mode)  # 保住原文件权限
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@app.route("/api/file_content", methods=["GET"])
def file_content():
    """读取文件供右侧面板查看/编辑。参数：path"""
    import os

    path = request.args.get("path", "")
    if not path:
        return jsonify({"ok": False, "error": "缺少 path 参数"}), 400

    # 先做权限校验，再判存在性：避免用「文件是否存在」探测白名单外的路径
    if not is_path_permitted(path):
        return jsonify({"ok": False, "error": f"拒绝访问：{path} 不在允许范围内"}), 403

    if not os.path.isfile(path):
        return jsonify({"ok": False, "error": f"不是文件：{path}"}), 400

    payload, status = _read_for_editor(path)
    return jsonify(payload), status


@app.route("/api/file_content", methods=["PUT"])
def save_file_content():
    """保存右侧面板里的编辑。请求：{path, content, expected_hash?}

    写之前会：
    1. 权限校验（复用 Web 白名单，敏感目录一律拒绝）
    2. **陈旧检测**：磁盘上的 hash 与 expected_hash 不符就返回 409。
       本项目的核心用途就是让 Agent 改你的文件，「你看编辑器时 Agent 正好写了
       同一个文件」是常态；没有这个检查，一次保存就会静默覆盖掉 Agent 的成果。
    3. **先备份进回收站**（可一键恢复）
    4. 原子写，并还原原来的换行符风格
    """
    import os

    import trash

    data = request.get_json(silent=True) or {}
    path = (data.get("path") or "").strip()
    content = data.get("content")
    expected_hash = (data.get("expected_hash") or "").strip() or None
    eol = (data.get("eol") or "lf").lower()

    if not path:
        return jsonify({"ok": False, "error": "缺少 path 参数"}), 400
    if content is None:
        return jsonify({"ok": False, "error": "缺少 content"}), 400

    if not is_path_permitted(path):
        return jsonify({"ok": False, "error": f"拒绝写入：{path} 不在允许范围内"}), 403
    if not os.path.isfile(path):
        return jsonify({"ok": False, "error": f"文件不存在：{path}"}), 400

    encoded = content.encode("utf-8")
    if len(encoded) > MAX_EDIT_BYTES:
        return jsonify(
            {"ok": False, "error": f"内容 {len(encoded) / 1024:.0f} KB，超过编辑上限"}
        ), 400

    if expected_hash:
        current = _sha256_file(path)
        if current != expected_hash:
            return jsonify(
                {
                    "ok": False,
                    "error": "stale",
                    "current_hash": current,
                    "message": "文件在磁盘上已被改动（可能是 Agent 刚写过），已拒绝覆盖。"
                    "请先「重新加载」确认最新内容，或选择强制覆盖。",
                }
            ), 409

    try:
        backup = trash.backup_file(path, kind="backup")
    except Exception:
        backup = None

    try:
        text = content.replace("\r\n", "\n")
        if eol == "crlf":
            text = text.replace("\n", "\r\n")
        _atomic_write_text(path, text)
    except Exception as e:
        return jsonify({"ok": False, "error": f"写入失败：{e}"}), 500

    stat = os.stat(path)
    return jsonify(
        {
            "ok": True,
            "path": path,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "hash": _sha256_file(path),
            "backup": os.path.basename(backup) if backup else None,
        }
    )


# ============ 页面 ============
@app.route("/")
def index():
    # 把访问令牌注入页面：跨域页面读不到本响应内容，因此不会因此泄露
    return render_template("index.html", auth_token=auth.get_token())


# ============ 会话接口 ============
@app.route("/sessions", methods=["GET"])
def list_sessions():
    """会话列表；带 workspace_id 时只返回该工作区下的会话。"""
    wid = request.args.get("workspace_id", type=int)
    return jsonify(db.get_all_sessions(wid))


@app.route("/sessions", methods=["POST"])
def new_session():
    """新建会话并归属到指定工作区。

    归属工作区后，这个会话的工作目录就由工作区决定（不再依赖前端 localStorage）。
    """
    data = request.get_json(silent=True) or {}
    wid = data.get("workspace_id")
    if wid is not None:
        try:
            wid = int(wid)
        except (TypeError, ValueError):
            wid = None
    if wid is not None and not db.get_workspace(wid):
        return jsonify({"error": "工作区不存在"}), 404

    sid = db.create_session("新对话", wid)
    # 回读实际归属：不传 workspace_id 时 db 会落到默认工作区，
    # 直接回请求里的值会返回 None，前端就不知道该把它挂在哪个工作区下了
    session = db.get_session(sid) or {}
    actual_wid = session.get("workspace_id")
    if actual_wid is not None:
        db.touch_workspace(actual_wid)
    return jsonify({"id": sid, "title": "新对话", "workspace_id": actual_wid})


# ============ 项目约定（AGENTS.md）============
def _workspace_root(workspace_id) -> str | None:
    if not workspace_id:
        return None
    try:
        ws = db.get_workspace(int(workspace_id))
    except (TypeError, ValueError):
        return None
    return (ws or {}).get("path") if isinstance(ws, dict) else None


@app.route("/api/project_context", methods=["GET"])
def get_project_context():
    """当前工作区加载到哪些 AGENTS.md、多大、预览。"""
    import project_context

    root = _workspace_root(request.args.get("workspace_id"))
    data = project_context.load(root)
    return jsonify({
        "ok": True,
        "workspace_root": root,
        "target_path": project_context.target_path(root),
        "sources": data["sources"],
        "truncated": data["truncated"],
        "chars": len(data["text"]),
        "preview": data["text"][:600],
    })


@app.route("/api/project_context", methods=["POST"])
def save_project_context():
    """新建 / 覆盖工作区根目录下的 AGENTS.md。

    只允许写在工作区根目录 —— 不提供任意路径写入，避免变成新的越权入口。
    """
    import project_context

    root = _workspace_root((request.get_json(silent=True) or {}).get("workspace_id"))
    if not root or not os.path.isdir(root):
        return jsonify({"ok": False, "error": "工作区路径不存在"}), 400

    content = (request.get_json(silent=True) or {}).get("content")
    if not isinstance(content, str):
        return jsonify({"ok": False, "error": "content 必须是字符串"}), 400

    target = project_context.target_path(root)
    try:
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        return jsonify({"ok": False, "error": f"写入失败：{e}"}), 500

    data = project_context.load(root)
    return jsonify({"ok": True, "path": target, "chars": len(data["text"]),
                    "sources": data["sources"]})


# ============ MCP 插件 ============
def real_tool_names(server_name: str) -> list:
    """该 server 提供的**真实工具**名（不含 resources/prompts 合成出来的）。"""
    import mcp_client

    names = []
    for entry in mcp_client.registry_snapshot():
        if entry["server"] == server_name and entry["kind"] == "tool":
            names.append(entry["name"])
    return sorted(names)


def _mcp_overview() -> dict:
    """服务器列表 + 运行状态。**不含任何密钥类字段**（env 只回键名）。"""
    import mcp_client
    import mcp_config

    servers = []
    for cfg in mcp_config.list_servers():
        status = mcp_client.server_status(cfg["name"])
        item = dict(cfg)
        # env 里可能有 token，只回键名不回值
        item["env_keys"] = sorted((cfg.get("env") or {}).keys())
        item.pop("env", None)
        # headers 里可能有 token，同样只回键名
        item["header_keys"] = sorted((cfg.get("headers") or {}).keys())
        item.pop("headers", None)
        item.update({
            "runtime_status": status["status"],
            "runtime_error": status["error"],
            "tool_count": status["tool_count"],
            "resource_count": status.get("resource_count", 0),
            "prompt_count": status.get("prompt_count", 0),
            "restart_count": status.get("restart_count", 0),
            "tools": real_tool_names(cfg["name"]),
        })
        servers.append(item)

    return {
        "servers": servers,
        "inject_enabled": mcp_config.load_config().get("inject_enabled", True),
        "config_path": mcp_config.MCP_FILE,
    }


@app.route("/api/mcp/servers", methods=["GET"])
def mcp_list():
    return jsonify({"ok": True, **_mcp_overview()})


@app.route("/api/mcp/servers", methods=["POST"])
def mcp_upsert():
    """新增或修改一个 server。**保存 ≠ 启用**：enabled 由请求显式给出，默认 false。"""
    import mcp_client
    import mcp_config

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    body = {k: v for k, v in data.items() if k != "name"}

    cfg, err = mcp_config.validate(name, body)
    if err:
        return jsonify({"ok": False, "error": err}), 400

    existed = mcp_config.get_server(name) is not None
    was_enabled = bool((mcp_config.get_server(name) or {}).get("enabled"))
    mcp_config.upsert_server(name, body)

    # 已经连着的，改动后重连一次让新配置生效
    if existed and was_enabled:
        mcp_client.disconnect(name)
        if cfg["enabled"]:
            ok, msg = mcp_client.connect(name)
            logger.info("MCP %s 重连：%s", name, msg)

    return jsonify({"ok": True, **_mcp_overview()})


@app.route("/api/mcp/servers/<name>", methods=["DELETE"])
def mcp_delete(name):
    import mcp_client
    import mcp_config

    mcp_client.disconnect(name)
    if not mcp_config.delete_server(name):
        return jsonify({"ok": False, "error": "没有这个 server"}), 404
    return jsonify({"ok": True, **_mcp_overview()})


@app.route("/api/mcp/servers/<name>/enabled", methods=["POST"])
def mcp_set_enabled(name):
    """启用 / 停用。启用时才真正去连（含起子进程）。"""
    import mcp_client
    import mcp_config

    if mcp_config.get_server(name) is None:
        return jsonify({"ok": False, "error": "没有这个 server"}), 404

    want = bool((request.get_json(silent=True) or {}).get("enabled"))
    mcp_config.patch_server(name, enabled=want)

    msg = ""
    if want:
        ok, msg = mcp_client.connect(name)
    else:
        mcp_client.disconnect(name)

    return jsonify({"ok": True, "message": msg, **_mcp_overview()})


@app.route("/api/mcp/servers/<name>/reconnect", methods=["POST"])
def mcp_reconnect(name):
    import mcp_client
    import mcp_config

    if mcp_config.get_server(name) is None:
        return jsonify({"ok": False, "error": "没有这个 server"}), 404
    mcp_client.disconnect(name)
    ok, msg = mcp_client.connect(name)
    return jsonify({"ok": ok, "message": msg, **_mcp_overview()})


@app.route("/api/mcp/catalog", methods=["GET"])
def mcp_catalog():
    """内置插件目录（含运行时是否可用）。"""
    import mcp_catalog

    return jsonify({"ok": True, "catalog": mcp_catalog.list_catalog()})


@app.route("/api/mcp/catalog/<item_id>/config", methods=["POST"])
def mcp_catalog_config(item_id):
    """把目录模板 + 占位符填值，生成一份插件配置（**不落盘**，前端确认后再提交）。"""
    import mcp_catalog

    data = request.get_json(silent=True) or {}
    cfg, err = mcp_catalog.build_config(item_id, data.get("values") or {}, data.get("name"))
    if err:
        return jsonify({"ok": False, "error": err}), 400
    cfg["name"] = data.get("name") or item_id
    return jsonify({"ok": True, "config": cfg})


@app.route("/api/mcp/inject", methods=["POST"])
def mcp_set_inject():
    """设置面板里的「MCP 工具注入」总开关。"""
    import mcp_config

    want = bool((request.get_json(silent=True) or {}).get("enabled"))
    mcp_config.set_inject_enabled(want)
    return jsonify({"ok": True, **_mcp_overview()})


# ============ 设置：provider 密钥 / URL ============
def _valid_base_url(url: str) -> bool:
    return url.startswith("http://") or url.startswith("https://")


@app.route("/api/settings/keys", methods=["POST"])
def save_provider_key():
    """保存某个 provider 的 api_key / base_url。

    **密钥只进不出**：这里收下后写进 ``data/providers.json``（权限 600），
    之后任何接口都只返回掩码（``sk-…abcd``），绝不回传完整密钥。
    """
    from config_providers import (
        PROVIDERS,
        clear_provider_credentials,
        set_provider_credentials,
    )

    data = request.get_json(silent=True) or {}
    pid = (data.get("provider_id") or "").strip()
    if pid not in PROVIDERS:
        return jsonify({"ok": False, "error": f"未知的 provider：{pid}"}), 400

    api_key = data.get("api_key")
    base_url = data.get("base_url")

    if isinstance(api_key, str):
        api_key = api_key.strip()
    else:
        api_key = None

    if isinstance(base_url, str):
        base_url = base_url.strip()
        if base_url and not _valid_base_url(base_url):
            return jsonify({"ok": False, "error": "Base URL 必须以 http:// 或 https:// 开头"}), 400
    else:
        base_url = None

    if api_key == "" and base_url in (None, ""):
        clear_provider_credentials(pid)
    else:
        set_provider_credentials(pid, api_key, base_url)

    # 日志里绝不出现密钥本身
    logger.info("更新 provider 配置：%s（key=%s）", pid, "已设置" if api_key else "未改动")
    return jsonify({"ok": True, "providers": get_provider_list()})


@app.route("/api/settings/keys/<provider_id>", methods=["DELETE"])
def delete_provider_key(provider_id):
    """清除界面里保存的密钥（.env 里的仍会生效）。"""
    from config_providers import PROVIDERS, clear_provider_credentials

    if provider_id not in PROVIDERS:
        return jsonify({"ok": False, "error": f"未知的 provider：{provider_id}"}), 400
    clear_provider_credentials(provider_id)
    return jsonify({"ok": True, "providers": get_provider_list()})


# ============ 会话权限等级 ============
PERMISSION_LABELS = {1: "只读", 2: "确认", 3: "完全"}


@app.route("/sessions/<int:session_id>/permission", methods=["POST"])
def set_session_permission(session_id):
    """设置会话权限等级：1 只读 / 2 确认 / 3 完全。

    等级是**服务端权威**的：存在 sessions 表里，run 时由后端按 session 读取，
    前端传什么都不作数——和「确认令牌在服务端」同一个思路。
    """
    if not db.get_session(session_id):
        return jsonify({"ok": False, "error": "会话不存在"}), 404

    data = request.get_json(silent=True) or {}
    try:
        level = int(data.get("level"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "level 必须是 1 / 2 / 3"}), 400
    if level not in (1, 2, 3):
        return jsonify({"ok": False, "error": "level 必须是 1 / 2 / 3"}), 400

    # 等级 3 允许 Agent 自动执行命令；若同时开了「宿主机执行」（而非 Docker 沙箱），
    # 就等于在自己家目录里无限制执行，风险不可接受，直接拒绝。
    if level == 3 and sandbox.ALLOW_HOST_EXEC:
        return jsonify(
            {
                "ok": False,
                "error": "已开启 AGENT_ALLOW_HOST_EXEC（宿主机执行），"
                "为安全起见禁止启用「等级 3 完全」；请改用 Docker 沙箱或选等级 2",
            }
        ), 400

    db.set_permission_level(session_id, level)
    return jsonify({"ok": True, "level": level, "label": PERMISSION_LABELS[level]})


@app.route("/sessions/<int:session_id>/permission", methods=["GET"])
def get_session_permission(session_id):
    if not db.get_session(session_id):
        return jsonify({"ok": False, "error": "会话不存在"}), 404
    level = db.get_permission_level(session_id)
    return jsonify(
        {
            "ok": True,
            "level": level,
            "label": PERMISSION_LABELS[level],
            "host_exec": sandbox.ALLOW_HOST_EXEC,
        }
    )


# ============ 工作区 ============
@app.route("/workspaces", methods=["GET"])
def list_workspaces():
    """工作区列表（附会话数、目录是否存在，以及新建时的默认路径）。"""
    return jsonify(
        {
            "workspaces": db.get_all_workspaces(),
            "default_path": db.default_workspace_path(),
        }
    )


@app.route("/workspaces", methods=["POST"])
def create_workspace():
    """新建工作区：`{name?, path?}`。

    path 留空时用默认位置（`~/PycharmProjects`，本机即 /home/pz/PycharmProjects）。
    """
    data = request.get_json(silent=True) or {}
    raw_path = (data.get("path") or "").strip()
    path = os.path.abspath(os.path.expanduser(raw_path)) if raw_path else db.default_workspace_path()

    if not paths.is_path_permitted(path):
        return jsonify({"ok": False, "error": f"该位置不允许作为工作区（敏感目录或越权）：{path}"}), 400

    # 目录不存在就建出来，避免用户选了个还没建的路径后到处报错
    if not os.path.isdir(path):
        try:
            os.makedirs(path, exist_ok=True)
        except Exception as e:
            return jsonify({"ok": False, "error": f"无法创建目录：{e}"}), 400

    name = (data.get("name") or "").strip() or os.path.basename(path.rstrip(os.sep)) or "工作区"
    wid = db.create_workspace(name, path)
    return jsonify({"ok": True, "id": wid, "name": name, "path": path})


@app.route("/workspaces/<int:workspace_id>/open_files", methods=["GET"])
def get_workspace_open_files(workspace_id):
    """该工作区在右侧文件面板里打开过的文件（供重启/切回时恢复）。"""
    if not db.get_workspace(workspace_id):
        return jsonify({"ok": False, "error": "工作区不存在"}), 404
    return jsonify({"ok": True, "paths": db.get_workspace_open_files(workspace_id)})


@app.route("/workspaces/<int:workspace_id>/open_files", methods=["PUT"])
def put_workspace_open_files(workspace_id):
    """保存该工作区打开的文件路径列表。

    只保留仍然合法（白名单内）的路径，避免记录里堆积已越权/失效的条目。
    """
    if not db.get_workspace(workspace_id):
        return jsonify({"ok": False, "error": "工作区不存在"}), 404
    data = request.get_json(silent=True) or {}
    paths = data.get("paths")
    if not isinstance(paths, list):
        return jsonify({"ok": False, "error": "paths 必须是数组"}), 400
    safe = [p for p in paths if isinstance(p, str) and is_path_permitted(p)]
    db.set_workspace_open_files(workspace_id, safe)
    return jsonify({"ok": True, "count": len(safe)})


@app.route("/workspaces/<int:workspace_id>", methods=["PATCH"])
def patch_workspace(workspace_id):
    """改名 / 改路径。"""
    if not db.get_workspace(workspace_id):
        return jsonify({"ok": False, "error": "工作区不存在"}), 404
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip() or None
    raw_path = (data.get("path") or "").strip()
    path = None
    if raw_path:
        path = os.path.abspath(os.path.expanduser(raw_path))
        if not paths.is_path_permitted(path):
            return jsonify({"ok": False, "error": f"该位置不允许作为工作区：{path}"}), 400
        if not os.path.isdir(path):
            try:
                os.makedirs(path, exist_ok=True)
            except Exception as e:
                return jsonify({"ok": False, "error": f"无法创建目录：{e}"}), 400
    db.update_workspace(workspace_id, name, path)
    return jsonify({"ok": True, "workspace": db.get_workspace(workspace_id)})


@app.route("/workspaces/<int:workspace_id>", methods=["DELETE"])
def remove_workspace(workspace_id):
    """删除工作区（连同其下会话）。前端会先提示将删除多少会话。"""
    if not db.get_workspace(workspace_id):
        return jsonify({"ok": False, "error": "工作区不存在"}), 404
    ok, result = db.delete_workspace(workspace_id)
    if not ok:
        return jsonify({"ok": False, "error": result}), 400
    # 清掉这些会话的粘性状态，避免内存泄漏
    import runtime as _rt_ws
    from tools import clear_plan

    for sid in result:
        _rt_ws.clear_session(sid)
        clear_plan(sid)
    return jsonify({"ok": True, "deleted_sessions": len(result)})


@app.route("/sessions/<int:session_id>", methods=["DELETE"])
def remove_session(session_id):
    db.delete_session(session_id)
    # 一并清理该会话的粘性状态（计划 / 待确认 / 项目根目录），避免内存泄漏
    import runtime
    from tools import clear_plan

    runtime.clear_session(session_id)
    clear_plan(session_id)
    return jsonify({"ok": True})


@app.route("/history/<int:session_id>", methods=["GET"])
def history(session_id):
    return jsonify(db.get_messages(session_id))


# ============ Agent 执行 ============
@app.route("/api/providers", methods=["GET"])
def api_providers():
    """返回可选的大模型提供商列表（不含 api_key）"""
    return jsonify({
        "providers": get_provider_list(),
        "default": DEFAULT_PROVIDER,
        "rag_provider": RAG_PROVIDER,
    })


@app.route("/run", methods=["POST"])
def run():
    data = request.get_json(silent=True) or {}
    task = data.get("task", "").strip()
    session_id = data.get("session_id")
    resume = data.get("resume", False)   # 用户点 ✓ 后“续跑”：复用 pending state，从原 step 继续
    # 注意：这里不再读取客户端传来的 confirmed_delete。
    # 「用户是否真的确认过」必须由服务端签发的一次性令牌证明（见 generate() 内的校验），
    # 否则任何人构造一个 confirmed_delete=true 就能跳过所有人工确认。

    if not task and not resume:
        return Response(
            "data: "
            + json.dumps({"type": "error", "content": "任务为空"}, ensure_ascii=False)
            + "\n\n",
            mimetype="text/event-stream",
        )

    if not session_id:
        return Response(
            "data: "
            + json.dumps(
                {"type": "error", "content": "缺少 session_id"}, ensure_ascii=False
            )
            + "\n\n",
            mimetype="text/event-stream",
        )

    def generate():
        # 绑定会话：本次运行的计划 / 待确认状态 / 项目根目录都落到该会话的桶里
        import runtime as _rt_run

        _rt_run.set_session_id(session_id)
        try:
            # 续跑分两种，门控不同：
            #  A. 挂起等待确认（pending 里有 pending_tool）→ 必须出示一次性确认令牌
            #  B. 用户主动中断后的「继续」（pending 里没有 pending_tool）→ 免令牌，
            #     因为它不会直接执行挂起的危险操作；后续危险操作仍会重新走确认流程
            if resume:
                _state = _rt_run.get_pending_state(session_id)
                if not _state:
                    yield _sse_error("没有可续跑的任务（可能已完成、已过期，或被新任务覆盖）")
                    return
                if _state.get("pending_tool") and not _rt_run.verify_and_consume_confirm_token(
                    session_id, data.get("confirm_token")
                ):
                    yield _sse_error("确认令牌无效或已过期，请重新发起任务")
                    return
            else:
                # 新任务开始：清掉上一轮残留的续跑点，避免「继续」接到旧状态
                _rt_run.clear_pending_state(session_id)

            # 保存用户任务（图片以 Base64 data URL 一起入库，刷新后历史仍可回显）
            images = data.get("images") or []   # 多模态图片（data URL 数组）
            if not resume:
                # 续跑（resume）时上一轮已存过用户消息，不再重复入库
                db.save_message(session_id, "user", _pack_user_content(task, images))
                # 自动命名：如果会话还是"新对话"，用任务前 20 字命名
                sessions = db.get_all_sessions()
                for s in sessions:
                    if s["id"] == session_id and s["title"] == "新对话":
                        new_title = task[:20] + ("..." if len(task) > 20 else "")
                        db.update_session_title(session_id, new_title)
                        break

            # 收集 AI 完整回答（用于保存）+ 工具执行记录
            full_answer = ""
            tool_events = []  # 记录工具调用，用于保存
            token_usage = None
            pending_confirm = False  # 本次运行是否因等待用户确认而中断

            # 新：优先用 provider_id 解析后端写死的 Key/URL/默认模型；
            # 旧：兼容前端仍直接传 api_key/base_url/model
            provider_id = data.get("provider_id")
            if provider_id:
                provider_cfg = get_provider(provider_id)
                user_api_key = provider_cfg["api_key"]
                user_base_url = provider_cfg["base_url"]
                user_model = data.get("model") or provider_cfg["default_model"]
            else:
                user_api_key = data.get("api_key")
                user_base_url = data.get("base_url")
                user_model = data.get("model")
            # 批次 5.5：前端不再传 rag_*，改成从 config_providers 拿 RAG 配置
            from config_providers import get_rag_config
            _rag_cfg = get_rag_config()
            rag_api_key = data.get("rag_api_key") or _rag_cfg["api_key"]
            rag_base_url = data.get("rag_base_url") or _rag_cfg["base_url"]
            project_root = data.get("project_root")
            if not project_root:
                # Web 端不再自己传项目路径（避免前端 localStorage 与后端按会话状态
                # 互为真相源、切会话时互相覆盖）。未显式指定时以「会话所属工作区」
                # 为准，并清掉历史遗留的按会话覆盖。
                _rt_run.clear_project_root(session_id)
            if images:
                print(f"[IMG] received {len(images)} images")   # 只记数量，绝不打印 Base64

            for event in run_agent_stream(
                task,
                session_id=session_id,
                provider_id=provider_id,
                # HTTP 层永不自动确认：每一次危险操作都必须单独走一次确认
                auto_confirm=False,
                _resume=resume,
                user_api_key=user_api_key,
                user_base_url=user_base_url,
                user_model=user_model,
                rag_api_key=rag_api_key,
                rag_base_url=rag_base_url,
                project_root=project_root,
                images=images,
                attachments=_load_attachments(data.get("attachments")),
            ):
                # 本次运行是否停在“等待用户确认”：此时不落库，等续跑那轮再保存完整回答
                if str(event.get("type", "")).startswith("confirm_"):
                    pending_confirm = True
                # 保存工具调用记录
                if event.get("type") == "tool_call":
                    tool_events.append(
                        {
                            "type": "tool_call",
                            "name": event.get("name"),
                            "args": event.get("args"),
                        }
                    )
                elif event.get("type") == "tool_result":
                    tool_events.append(
                        {
                            "type": "tool_result",
                            "content": event.get("content"),
                        }
                    )
                elif event.get("type") == "text":
                    full_answer += event.get("content", "")
                elif event.get("type") == "token_usage":  # ✅ 新增
                    token_usage = {
                        "prompt_tokens": event.get("prompt_tokens"),
                        "completion_tokens": event.get("completion_tokens"),
                        "total_tokens": event.get("total_tokens"),
                        "steps": event.get("steps"),
                    }

                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

            # 保存 AI 回答（含工具执行记录，用于后续恢复）
            if pending_confirm:
                # 本次运行因等待用户确认而中断：不落库，等续跑完成后由续跑那轮保存完整回答，
                # 避免历史里留下“（已执行工具，无文本输出）”的残缺气泡。
                pass
            else:
                # 即使 full_answer 为空也保存，避免"只调工具没输出"时丢消息
                saved_answer = full_answer if full_answer else "（已执行工具，无文本输出）"
                saved = json.dumps(
                    {
                        "answer": saved_answer,
                        "tools": tool_events,
                        "tokens": token_usage,
                    },
                    ensure_ascii=False,
                )
                db.save_message(session_id, "assistant", saved)
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)}, ensure_ascii=False)}\n\n"
        finally:
            # 正常结束 / 异常 / 客户端断开都要注销取消标志，避免状态残留
            _rt_run.clear_run(session_id)

    return Response(generate(), mimetype="text/event-stream")


@app.route("/run/cancel", methods=["POST"])
def cancel_run():
    """请求中断某个会话正在执行的任务。

    协作式取消：设置该会话的取消标志，Agent 主循环会在下一个检查点
    （下一步开始 / 下一段流式输出 / 下一个工具执行前）停下来。
    """
    import runtime

    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id")
    if session_id is None:
        return jsonify({"error": "缺少 session_id"}), 400

    hit = runtime.request_cancel(session_id)
    return jsonify({"ok": True, "cancelled": hit})


@app.route("/export/<int:session_id>", methods=["GET"])
def export_session(session_id):
    """导出会话（支持 md / json / pdf）"""
    from datetime import datetime
    from urllib.parse import quote

    fmt = request.args.get("format", "md").lower()

    # 找会话标题
    sessions = db.get_all_sessions()
    title = "对话记录"
    for s in sessions:
        if s["id"] == session_id:
            title = s["title"]
            break

    safe_title = title.replace("/", "_").replace("\\", "_").replace(":", "_")
    messages = db.get_messages(session_id)

    # 解析 AI 消息的 JSON
    parsed = []
    for m in messages:
        content = m["content"]
        if m["role"] == "assistant":
            try:
                import json as _json

                obj = _json.loads(content)
                if "answer" in obj:
                    content = obj["answer"]
            except Exception:
                pass
        elif m["role"] == "user":
            # 带图片的用户消息存的是 JSON，导出时只取文本并标注图片数量
            try:
                import json as _json

                obj = _json.loads(content)
                if isinstance(obj, dict) and "text" in obj:
                    img_count = len(obj.get("images") or [])
                    content = obj.get("text", "")
                    if img_count:
                        content += f"\n\n（含 {img_count} 张图片）"
            except Exception:
                pass
        parsed.append(
            {
                "role": m["role"],
                "content": content,
                "created_at": m["created_at"],
            }
        )

    # ===== JSON 格式 =====
    if fmt == "json":
        import json as _json

        data = {
            "title": title,
            "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "messages": parsed,
        }
        text = _json.dumps(data, ensure_ascii=False, indent=2)
        filename = quote(f"{safe_title}.json")
        return Response(
            text,
            mimetype="application/json",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
        )

    # ===== Markdown 格式（默认）=====
    if fmt == "md":
        lines = [
            f"# {title}",
            "",
            f"> 导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "---",
            "",
        ]
        for m in parsed:
            role_icon = "🧑 用户" if m["role"] == "user" else "🤖 AI"
            lines.append(f"## {role_icon}")
            lines.append(f"*{m['created_at']}*")
            lines.append("")
            lines.append(m["content"])
            lines.append("")
            lines.append("---")
            lines.append("")

        md_text = "\n".join(lines)
        filename = quote(f"{safe_title}.md")
        return Response(
            md_text,
            mimetype="text/markdown",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
        )

    if fmt == "pdf":
        try:
            import io
            import re

            from reportlab.lib.pagesizes import A4
            from reportlab.lib.units import cm
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.cidfonts import UnicodeCIDFont
            from reportlab.pdfgen import canvas

            pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))

            def clean_text(s):
                # 去掉 emoji 和特殊字符
                return re.sub(
                    r"[^\u4e00-\u9fff\u3000-\u303f\uff00-\uffef"
                    r'a-zA-Z0-9\s\.\,\!\?\:\;\-\(\)\[\]\{\}\'"'
                    r"\+\=\*\/\%\#\@\&\|\~\`\<\>\n]",
                    "",
                    s,
                )

            buf = io.BytesIO()
            c = canvas.Canvas(buf, pagesize=A4)
            width, height = A4

            c.setFont("STSong-Light", 14)
            c.drawString(2 * cm, height - 2 * cm, clean_text(title))

            c.setFont("STSong-Light", 9)
            c.drawString(
                2 * cm,
                height - 2.5 * cm,
                clean_text(f"导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"),
            )

            y = height - 3.5 * cm
            for m in parsed:
                if y < 2 * cm:
                    c.showPage()
                    c.setFont("STSong-Light", 9)
                    y = height - 2 * cm

                role = "用户" if m["role"] == "user" else "AI"
                c.setFont("STSong-Light", 10)
                c.drawString(2 * cm, y, clean_text(f"[{role}] {m['created_at']}"))
                y -= 0.5 * cm

                c.setFont("STSong-Light", 9)
                for line in clean_text(m["content"]).split("\n")[:50]:
                    for chunk in [line[i : i + 50] for i in range(0, len(line), 50)]:
                        if y < 2 * cm:
                            c.showPage()
                            c.setFont("STSong-Light", 9)
                            y = height - 2 * cm
                        c.drawString(2.3 * cm, y, chunk)
                        y -= 0.4 * cm
                y -= 0.5 * cm

            c.save()
            buf.seek(0)

            filename = quote(f"{safe_title}.pdf")
            return Response(
                buf.read(),
                mimetype="application/pdf",
                headers={
                    "Content-Disposition": f"attachment; filename*=UTF-8''{filename}"
                },
            )
        except Exception as e:
            import traceback

            traceback.print_exc()
            return jsonify({"error": f"PDF 生成失败：{e}"}), 500


@app.route("/upload_doc", methods=["POST"])
def upload_doc():
    """上传文档（txt/md/pdf/docx），提取文本 → 切片 → 向量化 → 入库"""
    import rag

    if "file" not in request.files:
        return jsonify({"error": "没有文件"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "文件名为空"}), 400

    # ✅ 放开后缀限制，具体校验交给 rag.extract_text
    allowed = (".txt", ".md", ".pdf", ".docx")
    if not file.filename.lower().endswith(allowed):
        return jsonify({"error": f"只支持 {' / '.join(allowed)} 文件"}), 400

    # ✅ 读字节，不再手动 decode
    file_bytes = file.read()
    if not file_bytes:
        return jsonify({"error": "文件内容为空"}), 400

    # 大文件保护（比如 20MB）
    MAX_UPLOAD = 20 * 1024 * 1024
    if len(file_bytes) > MAX_UPLOAD:
        return jsonify({"error": f"文件太大（>{MAX_UPLOAD // 1024 // 1024}MB）"}), 400

    # ✅ 提取文本（这一步统一处理格式）
    try:
        content = rag.extract_text(file.filename, file_bytes)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"解析失败：{e}"}), 500

    if not content.strip():
        return jsonify({"error": "文件内容为空（提取不到文字）"}), 400

    import runtime
    user_api_key = request.form.get("api_key") or request.headers.get("X-API-Key") or ""
    user_base_url = request.form.get("base_url") or ""
    rag_api_key = request.form.get("rag_api_key") or ""
    rag_base_url = request.form.get("rag_base_url") or ""
    if user_api_key:
        runtime.set_api_key(user_api_key)
    if user_base_url:
        runtime.set_base_url(user_base_url)
    if rag_api_key:
        runtime.set_rag_api_key(rag_api_key)
    if rag_base_url:
        runtime.set_rag_base_url(rag_base_url)

    # 入库
    doc_id = None
    try:
        doc_id = db.add_document(file.filename, 0)
        # RAG 锁死用 config_providers 的 RAG 配置（bailian），不再 fallback 到主 key
        from config_providers import get_rag_config
        rag_cfg = get_rag_config()
        chunk_count = rag.add_document_to_vectorstore(
            doc_id,
            file.filename,
            content,
            api_key=rag_api_key or rag_cfg["api_key"],
            base_url=rag_base_url or rag_cfg["base_url"],
        )
        db.update_document_chunk_count(doc_id, chunk_count)
    except Exception as e:
        # 回滚
        if doc_id is not None:
            try:
                db.delete_document(doc_id)
                rag.delete_document_from_vectorstore(doc_id)
            except Exception:
                pass
        return jsonify({"error": f"处理失败：{str(e)}"}), 500

    return jsonify({
        "ok": True,
        "doc_id": doc_id,
        "filename": file.filename,
        "chunk_count": chunk_count,
        "char_count": len(content),
    })

@app.route("/documents", methods=["GET"])
def list_documents():
    """列出所有文档"""
    return jsonify(db.get_all_documents())


@app.route("/documents/<int:doc_id>", methods=["DELETE"])
def remove_document(doc_id):
    """删除文档及其向量"""
    import rag

    try:
        rag.delete_document_from_vectorstore(doc_id)
        db.delete_document(doc_id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============ 回收站（一键恢复）============
@app.route("/api/trash", methods=["GET"])
def api_trash_list():
    """列出回收站里的备份（含原始路径，可一键恢复）。"""
    import trash

    entries = trash.list_entries()
    return jsonify(
        {
            "ok": True,
            "entries": entries,
            "total_size": sum(e["size"] for e in entries),
        }
    )


@app.route("/api/trash/restore", methods=["POST"])
def api_trash_restore():
    """把某条备份恢复到它的原始路径。"""
    import trash

    data = request.get_json(silent=True) or {}
    ok, message = trash.restore(data.get("id", ""))
    if ok:
        return jsonify({"ok": True, "message": message})
    return jsonify({"ok": False, "message": message}), 400


@app.route("/api/trash/purge", methods=["POST"])
def api_trash_purge_many():
    """批量彻底删除回收站记录（前端「删除选中 / 清空」用）。

    单条失败不影响其余条目，返回实际删除数与失败原因，便于前端如实提示。
    """
    import trash as _trash

    data = request.get_json(silent=True) or {}
    ids = data.get("ids")
    if not isinstance(ids, list) or not ids:
        return jsonify({"ok": False, "removed": 0, "message": "未选择任何条目"}), 400

    removed, failed = 0, []
    for entry_id in ids:
        ok, msg = _trash.purge(str(entry_id))
        if ok:
            removed += 1
        else:
            failed.append(msg)

    message = f"已彻底删除 {removed} 条"
    if failed:
        message += f"，{len(failed)} 条失败"
    return jsonify(
        {
            "ok": removed > 0,
            "removed": removed,
            "failed": failed,
            "message": message,
        }
    )


@app.route("/api/trash/<path:entry_id>", methods=["DELETE"])
def api_trash_purge(entry_id):
    """彻底删除某条回收站记录（不可恢复）。"""
    import trash

    ok, message = trash.purge(entry_id)
    if ok:
        return jsonify({"ok": True, "message": message})
    return jsonify({"ok": False, "message": message}), 400


@app.route("/run_multi", methods=["POST"])
def run_multi():
    """多 Agent 协作模式：Planner 规划 → Executor 执行 → Reviewer 审查"""
    from planner import create_plan
    from reviewer import review

    data = request.get_json(silent=True) or {}
    task = data.get("task", "").strip()
    session_id = data.get("session_id")
    resume = data.get("resume", False)   # 用户点 ✓ 后“续跑”：复用 pending state，从原 step 继续

    if (not task and not resume) or not session_id:
        return jsonify({"error": "缺少参数"}), 400

    def generate():
        # 绑定会话：Planner 写入的计划、Executor 的 pending 都归属该会话
        import runtime as _rt_multi

        _rt_multi.set_session_id(session_id)
        # 与 /run 一致：多 Agent 模式也以「会话所属工作区」为工作目录
        if not (data.get("project_root") or "").strip():
            _rt_multi.clear_project_root(session_id)
        try:
            # 续跑门控与 /run 一致：挂起确认要令牌，纯「继续」免令牌
            if resume:
                _state = _rt_multi.get_pending_state(session_id)
                if not _state:
                    yield _sse_error("没有可续跑的任务（可能已完成、已过期，或被新任务覆盖）")
                    return
                if _state.get("pending_tool") and not _rt_multi.verify_and_consume_confirm_token(
                    session_id, data.get("confirm_token")
                ):
                    yield _sse_error("确认令牌无效或已过期，请重新发起任务")
                    return
            else:
                _rt_multi.clear_pending_state(session_id)

            # 批次 6：三角色各自可以独立选 provider，默认继承 executor 的
            executor_provider_id = data.get("provider_id") or DEFAULT_PROVIDER
            planner_provider_id = data.get("planner_provider_id") or executor_provider_id
            reviewer_provider_id = data.get("reviewer_provider_id") or executor_provider_id

            executor_cfg = get_provider(executor_provider_id)
            planner_cfg = get_provider(planner_provider_id)
            reviewer_cfg = get_provider(reviewer_provider_id)

            user_api_key = executor_cfg["api_key"]
            user_base_url = executor_cfg["base_url"]
            executor_model = data.get("executor_model") or executor_cfg["default_model"]

            planner_model = data.get("planner_model") or planner_cfg["default_model"]
            planner_api_key = planner_cfg["api_key"]
            planner_base_url = planner_cfg["base_url"]

            reviewer_model = data.get("reviewer_model") or reviewer_cfg["default_model"]
            reviewer_api_key = reviewer_cfg["api_key"]
            reviewer_base_url = reviewer_cfg["base_url"]

            # 批次 5：前端不再传 rag_*，改成从 config_providers 拿 RAG 配置
            from config_providers import get_rag_config
            _rag_cfg = get_rag_config()
            rag_api_key = data.get("rag_api_key") or _rag_cfg["api_key"]
            rag_base_url = data.get("rag_base_url") or _rag_cfg["base_url"]
            images = data.get("images") or []   # 多模态图片（data URL 数组）
            if images:
                print(f"[IMG] received {len(images)} images")   # 只记数量，绝不打印 Base64

            if not resume:
                # ========== Step 1: Planner 规划 ==========
                yield f"data: {json.dumps({'type': 'phase', 'name': 'planner', 'status': 'doing'}, ensure_ascii=False)}\n\n"
                plan = create_plan(
                    task,
                    api_key=planner_api_key,
                    base_url=planner_base_url,
                    model=planner_model,
                )
                yield f"data: {json.dumps({'type': 'plan_from_planner', 'plan': plan}, ensure_ascii=False)}\n\n"

                if plan.get("error"):
                    error_msg = f"⚠️ 规划失败：{plan.get('error')}，降级为单 Agent 模式"
                    yield f"data: {json.dumps({'type': 'text', 'content': error_msg}, ensure_ascii=False)}\n\n"

                steps = plan.get("steps", [task])
                # 把 Planner 的计划同步到 tools 内存
                try:
                    import tools

                    tools.create_plan(steps)
                except Exception as e:
                    yield f"data: {json.dumps({'type': 'text', 'content': f'⚠️ 同步计划失败：{e}'}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'phase', 'name': 'planner', 'status': 'done'}, ensure_ascii=False)}\n\n"
            else:
                # 续跑：不重新规划，plan 从 pending_state 恢复
                steps = None   # 会在 run_agent_stream 里从 pending 恢复
                yield f"data: {json.dumps({'type': 'phase', 'name': 'planner', 'status': 'done'}, ensure_ascii=False)}\n\n"

            # ========== Step 2: Executor 执行 ==========
            yield f"data: {json.dumps({'type': 'phase', 'name': 'executor', 'status': 'doing'}, ensure_ascii=False)}\n\n"

            # 保存用户任务（图片以 Base64 data URL 一起入库）
            # 续跑（resume）时上一轮已存过用户消息，不再重复入库
            if not resume:
                db.save_message(session_id, "user", _pack_user_content(task, images))

            full_answer = ""
            tool_events = []
            token_usage = None

            # ✅ 关键改动：把 Planner 的计划传给 Executor
            for event in run_agent_stream(
                task,
                session_id=session_id,
                planner_plan=steps,
                provider_id=executor_provider_id,
                _resume=resume,   # 续跑：复用 pending state，从原 step 继续
                user_api_key=user_api_key,
                user_base_url=user_base_url,
                user_model=executor_model,
                rag_api_key=rag_api_key,
                rag_base_url=rag_base_url,
                images=images,
            ):
                # 续跑时若再次触发确认：转发确认事件后立即结束，不再往下走 Reviewer
                if str(event.get("type", "")).startswith("confirm_"):
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    return
                # 转发事件
                if event.get("type") == "tool_call":
                    tool_events.append(
                        {
                            "type": "tool_call",
                            "name": event.get("name"),
                            "args": event.get("args"),
                        }
                    )
                elif event.get("type") == "tool_result":
                    tool_events.append(
                        {
                            "type": "tool_result",
                            "content": event.get("content"),
                        }
                    )
                elif event.get("type") == "text":
                    full_answer += event.get("content", "")
                elif event.get("type") == "token_usage":
                    token_usage = event

                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

            yield f"data: {json.dumps({'type': 'phase', 'name': 'executor', 'status': 'done'}, ensure_ascii=False)}\n\n"

            # 用户已请求停止：保存已产出的内容，但不再进入 Reviewer 审查
            if _rt_multi.is_cancelled():
                saved = json.dumps(
                    {
                        "answer": full_answer,
                        "tools": tool_events,
                        "tokens": token_usage,
                    },
                    ensure_ascii=False,
                )
                db.save_message(session_id, "assistant", saved)
                yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
                return

            # 保存 Executor 结果
            saved = json.dumps(
                {
                    "answer": full_answer,
                    "tools": tool_events,
                    "tokens": token_usage,
                },
                ensure_ascii=False,
            )
            db.save_message(session_id, "assistant", saved)

            # ========== Step 3: Reviewer 审查 ==========
            yield f"data: {json.dumps({'type': 'phase', 'name': 'reviewer', 'status': 'doing'}, ensure_ascii=False)}\n\n"
            review_result = review(
                task,
                full_answer,
                evidence=_build_evidence(tool_events),
                api_key=reviewer_api_key,
                base_url=reviewer_base_url,
                model=reviewer_model,
            )
            yield f"data: {json.dumps({'type': 'review_result', 'result': review_result}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'phase', 'name': 'reviewer', 'status': 'done'}, ensure_ascii=False)}\n\n"

            yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)}, ensure_ascii=False)}\n\n"
        finally:
            _rt_multi.clear_run(session_id)

    return Response(generate(), mimetype="text/event-stream")


if __name__ == "__main__":
    # 默认关闭 debug：Werkzeug 交互式调试器不应默认暴露
    app.run(host="127.0.0.1", port=5004, debug=os.getenv("FLASK_DEBUG") == "1")
