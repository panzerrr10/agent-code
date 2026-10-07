import json
import os
from flask import Flask, request, render_template, Response, jsonify
import db
from agent import run_agent_stream
from paths import ICON_PATH, is_path_permitted
from config_providers import get_provider, get_provider_list, DEFAULT_PROVIDER, RAG_PROVIDER

app = Flask(__name__)
db.init_db()

from flask import send_file


def _pack_user_content(task: str, images=None) -> str:
    """把用户消息打包成可写入 SQLite 的字符串。

    - 有图片：存 JSON（{"text": ..., "images": [data URL, ...]}），
      图片以 Base64 data URL 内联，刷新页面 / 切换会话后仍能从历史回显。
    - 无图片：保持纯文本，兼容历史旧数据与导出、上下文裁剪逻辑。
    """
    if images:
        return json.dumps({"text": task, "images": images}, ensure_ascii=False)
    return task


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


@app.route("/api/select_folder", methods=["POST"])
def select_folder():
    """弹系统"选择文件夹"对话框，返回绝对路径。
    注意：这个 API 只在桌面环境下有效（有 tkinter + 显示）。
    """
    try:
        import tkinter as tk
        from tkinter import filedialog

        # 创建隐藏的根窗口
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)  # 弹窗置顶

        folder = filedialog.askdirectory(
            title="选择项目文件夹",
            mustexist=True,
        )

        root.destroy()

        if not folder:
            return jsonify({"ok": False, "error": "未选择文件夹"})

        return jsonify({"ok": True, "path": folder})
    except Exception as e:
        return jsonify({"ok": False, "error": f"弹窗失败：{e}"}), 500


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


@app.route("/api/file_content", methods=["GET"])
def file_content():
    """返回文件内容（用于预览）。
    参数：path
    """
    import os
    path = request.args.get("path", "")
    if not path:
        return jsonify({"error": "缺少 path 参数"}), 400

    # 先做权限校验，再判存在性：避免用「文件是否存在」探测白名单外的路径
    if not is_path_permitted(path):
        return jsonify({"error": f"拒绝访问：{path} 不在允许范围内"}), 403

    if not os.path.isfile(path):
        return jsonify({"error": f"不是文件：{path}"}), 400

    # 限制大小
    size = os.path.getsize(path)
    if size > 100_000:
        return jsonify({"error": f"文件太大（{size} 字节），无法预览"}), 400

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        return jsonify({"ok": True, "content": content, "size": size})
    except Exception as e:
        return jsonify({"error": f"读取失败：{e}"}), 500


# ============ 页面 ============
@app.route("/")
def index():
    return render_template("index.html")


# ============ 会话接口 ============
@app.route("/sessions", methods=["GET"])
def list_sessions():
    return jsonify(db.get_all_sessions())


@app.route("/sessions", methods=["POST"])
def new_session():
    sid = db.create_session("新对话")
    return jsonify({"id": sid, "title": "新对话"})


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
    data = request.json
    task = data.get("task", "").strip()
    session_id = data.get("session_id")
    confirmed_delete = data.get("confirmed_delete", False)
    resume = data.get("resume", False)   # 用户点 ✓ 后“续跑”：复用 pending state，从原 step 继续

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
            if images:
                print(f"[IMG] received {len(images)} images")   # 只记数量，绝不打印 Base64

            for event in run_agent_stream(
                task,
                session_id=session_id,
                provider_id=provider_id,
                confirmed_delete=confirmed_delete,
                _resume=resume,
                user_api_key=user_api_key,
                user_base_url=user_base_url,
                user_model=user_model,
                rag_api_key=rag_api_key,
                rag_base_url=rag_base_url,
                project_root=project_root,
                images=images,
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

    return Response(generate(), mimetype="text/event-stream")


@app.route("/export/<int:session_id>", methods=["GET"])
def export_session(session_id):
    """导出会话（支持 md / json / pdf）"""
    from urllib.parse import quote
    from datetime import datetime

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
            from reportlab.lib.pagesizes import A4
            from reportlab.pdfgen import canvas
            from reportlab.lib.units import cm
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.cidfonts import UnicodeCIDFont
            import io
            import re

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


@app.route("/run_multi", methods=["POST"])
def run_multi():
    """多 Agent 协作模式：Planner 规划 → Executor 执行 → Reviewer 审查"""
    from planner import create_plan
    from reviewer import review

    data = request.json
    task = data.get("task", "").strip()
    session_id = data.get("session_id")
    resume = data.get("resume", False)   # 用户点 ✓ 后“续跑”：复用 pending state，从原 step 继续

    if (not task and not resume) or not session_id:
        return jsonify({"error": "缺少参数"}), 400

    def generate():
        # 绑定会话：Planner 写入的计划、Executor 的 pending 都归属该会话
        import runtime as _rt_multi

        _rt_multi.set_session_id(session_id)
        try:
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

    return Response(generate(), mimetype="text/event-stream")


if __name__ == "__main__":
    # 默认关闭 debug：Werkzeug 交互式调试器不应默认暴露
    app.run(host="127.0.0.1", port=5004, debug=os.getenv("FLASK_DEBUG") == "1")
