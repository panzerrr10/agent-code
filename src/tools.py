import os
import shlex
import subprocess

import rag
import sandbox
import trash
from config import MAX_FILE_SIZE, MAX_LIST_ITEMS
from paths import is_allowed_path
from state_store import SessionStateStore

# ============ 工具实现 ============


def _normalize_path(path: str) -> str:
    import runtime
    workspace = runtime.get_project_root()
    if path == "/workspace":
        return workspace
    if path.startswith("/workspace/"):
        return os.path.join(workspace, path[len("/workspace/"):])
    if os.path.isabs(path):
        return path
    return os.path.join(workspace, path)


def list_dir(path: str = ".") -> str:
    """列出目录内容"""
    path = _normalize_path(path)
    if not is_allowed_path(path):
        return f"❌ 拒绝访问：{path} 不在白名单内"

    try:
        abs_path = os.path.realpath(path)
        if not os.path.isdir(abs_path):
            return f"❌ 不是目录：{path}"

        items = []
        for name in sorted(os.listdir(abs_path)):
            full = os.path.join(abs_path, name)
            if os.path.isdir(full):
                items.append(f"📁 {name}/")
            else:
                size = os.path.getsize(full)
                items.append(f"📄 {name}  ({size} bytes)")

            if len(items) >= MAX_LIST_ITEMS:
                items.append(f"...（超过 {MAX_LIST_ITEMS} 项，已截断）")
                break

        if not items:
            return f"目录为空：{path}"
        return f"目录 {path} 的内容：\n" + "\n".join(items)
    except Exception as e:
        return f"❌ 列出失败：{e}"


def read_file(path: str, max_lines: int = 500) -> str:
    """读取文件内容"""
    path = _normalize_path(path)
    if not is_allowed_path(path):
        return f"❌ 拒绝访问：{path} 不在白名单内"

    try:
        abs_path = os.path.realpath(path)
        if not os.path.isfile(abs_path):
            return f"❌ 不是文件：{path}"

        size = os.path.getsize(abs_path)
        if size > MAX_FILE_SIZE:
            return f"❌ 文件太大（{size} bytes），超过限制 {MAX_FILE_SIZE}"

        with open(abs_path, encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

        total = len(lines)
        lines = lines[:max_lines]
        content = "".join(lines)

        header = f"文件：{path}（共 {total} 行，显示前 {len(lines)} 行）\n"
        header += "=" * 50 + "\n"
        return header + content
    except Exception as e:
        return f"❌ 读取失败：{e}"


def read_file_range(path: str, start: int = 1, end: int = 100) -> str:
    """
    读文件第 start 到 end 行（1-based）。
    适合改大文件时局部查看。
    """
    path = _normalize_path(path)
    if not is_allowed_path(path):
        return f"❌ 拒绝访问：{path} 不在白名单内"

    try:
        abs_path = os.path.realpath(path)
        if not os.path.isfile(abs_path):
            return f"❌ 不是文件：{path}"

        with open(abs_path, encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

        total = len(lines)

        # 边界
        start = max(1, start)
        end = min(total, end)

        if start > end:
            return "❌ 起始行 > 结束行"

        selected = lines[start - 1 : end]
        content = "".join(selected)

        header = f"文件：{path}（共 {total} 行，第 {start}-{end} 行）\n"
        header += "=" * 50 + "\n"
        return header + content
    except Exception as e:
        return f"❌ 读取失败：{e}"


def search_files(keyword: str, path: str = ".", max_results: int = 50) -> str:
    """在目录下搜索包含关键词的文件"""
    path = _normalize_path(path)
    if not is_allowed_path(path):
        return f"❌ 拒绝访问：{path} 不在白名单内"

    try:
        abs_path = os.path.realpath(path)
        if not os.path.isdir(abs_path):
            return f"❌ 不是目录：{path}"

        results = []
        for root, dirs, files in os.walk(abs_path):
            # 跳过常见无关目录
            dirs[:] = [
                d
                for d in dirs
                if d
                not in {
                    ".git",
                    "__pycache__",
                    "node_modules",
                    ".venv",
                    "venv",
                    "chroma_db",
                }
            ]

            for fname in files:
                fpath = os.path.join(root, fname)
                # 只搜文本文件
                if not fname.endswith(
                    (
                        ".py",
                        ".txt",
                        ".md",
                        ".json",
                        ".html",
                        ".css",
                        ".js",
                        ".yaml",
                        ".yml",
                        ".toml",
                    )
                ):
                    continue
                try:
                    with open(fpath, encoding="utf-8", errors="ignore") as f:
                        for i, line in enumerate(f, 1):
                            if keyword in line:
                                rel = os.path.relpath(fpath, abs_path)
                                results.append(f"{rel}:{i}: {line.strip()[:100]}")
                                if len(results) >= max_results:
                                    break
                    if len(results) >= max_results:
                        break
                except Exception:
                    continue
            if len(results) >= max_results:
                break

        if not results:
            return f"未找到包含 '{keyword}' 的内容"
        return f"找到 {len(results)} 处匹配：\n" + "\n".join(results)
    except Exception as e:
        return f"❌ 搜索失败：{e}"


def get_file_info(path: str) -> str:
    """获取文件/目录的详细信息"""
    path = _normalize_path(path)
    if not is_allowed_path(path):
        return f"❌ 拒绝访问：{path} 不在白名单内"

    try:
        abs_path = os.path.realpath(path)
        if not os.path.exists(abs_path):
            return f"❌ 路径不存在：{path}"

        stat = os.stat(abs_path)
        import datetime

        mtime = datetime.datetime.fromtimestamp(stat.st_mtime).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        info = [
            f"路径：{abs_path}",
            f"类型：{'目录' if os.path.isdir(abs_path) else '文件'}",
            f"大小：{stat.st_size} bytes",
            f"修改时间：{mtime}",
        ]
        return "\n".join(info)
    except Exception as e:
        return f"❌ 获取信息失败：{e}"


# ===== 本轮改动记录（供「第 N 轮改动」+ diff 查看）=====
# 单文件 diff 的上限。**必须有限制** —— 改一个几千行的文件能生成上万行 diff，
# 既拖慢接口也会把前端渲染卡住。
MAX_DIFF_LINES = 4000
MAX_DIFF_CHARS = 256 * 1024


def _read_text_safe(path) -> str:
    """尽力读文本；读不到就当空（二进制/权限问题不该让记录流程崩掉）。"""
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _make_diff(path: str, before: str, after: str):
    """生成 unified diff，返回 (文本, 新增行数, 删除行数, 是否截断)。

    用标准库 ``difflib``，**不要自己写 LCS** —— 前端的朴素 DP 是 O(m×n)，
    几千行的文件就是几千万格，会把浏览器卡死。
    """
    import difflib

    name = os.path.basename(path)
    lines = list(difflib.unified_diff(
        (before or "").splitlines(keepends=True),
        (after or "").splitlines(keepends=True),
        fromfile=f"a/{name}", tofile=f"b/{name}", n=3,
    ))
    added = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
    text = "".join(lines)
    truncated = False
    if len(lines) > MAX_DIFF_LINES or len(text) > MAX_DIFF_CHARS:
        text = "".join(lines[:MAX_DIFF_LINES])[:MAX_DIFF_CHARS]
        truncated = True
    return text, added, removed, truncated


def _record_change(abs_path: str, change_type: str, tool: str,
                   before: str, after: str) -> None:
    """记一条本轮改动。

    **拿不到会话/轮次时静默跳过**：CLI 单次运行、评估集、单元测试里没有
    「轮次」这个概念，不该因为记录失败而影响工具本身的返回值。
    """
    try:
        import runtime

        sid = runtime.get_session_id()
        ridx = runtime.get_run_index()
        if not sid or not ridx:
            return
        import db

        text, added, removed, truncated = _make_diff(abs_path, before, after)
        db.record_change(sid, ridx, abs_path, change_type, tool,
                         text, added, removed, truncated)
    except Exception:                                       # noqa: BLE001
        import logging

        logging.getLogger(__name__).warning("记录本轮改动失败（不影响工具结果）", exc_info=True)


def create_file(path: str, content: str) -> str:
    """新建文件。如果文件已存在，拒绝执行（保护已有文件）"""
    path = _normalize_path(path)
    if not is_allowed_path(path):
        return f"❌ 拒绝访问：{path} 不在白名单内"

    try:
        abs_path = os.path.realpath(path)

        # 自动创建父目录
        parent = os.path.dirname(abs_path)
        if not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)

        # 检查文件是否已存在
        if os.path.exists(abs_path):
            return f"❌ 文件已存在，拒绝覆盖：{path}"

        # 限制文件大小
        if len(content) > MAX_FILE_SIZE:
            return f"❌ 内容太大（{len(content)} 字符），超过限制"

        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)

        _record_change(abs_path, "create", "create_file", "", content)
        return f"✅ 已创建文件：{path}（{len(content)} 字符）"
    except Exception as e:
        return f"❌ 创建失败：{e}"


def delete_file(path: str) -> str:
    """把文件移到项目里的 .trash 目录（不真删，可恢复）"""
    path = _normalize_path(path)
    if not is_allowed_path(path):
        return f"❌ 拒绝访问：{path} 不在白名单内"

    try:
        abs_path = os.path.realpath(path)

        if not os.path.exists(abs_path):
            return f"❌ 文件不存在：{path}"

        # 只允许删文件，不允许删目录
        if os.path.isdir(abs_path):
            return f"❌ 只能删除文件，不能删除目录：{path}"

        # 读一份内容：删除也要能在「第 N 轮改动」里看出删了什么
        before_text = _read_text_safe(abs_path)

        # 移入回收站（同时写 sidecar 记录原始路径，前端可一键恢复）
        trash_path = trash.move_to_trash(abs_path)
        if not trash_path:
            return "❌ 删除失败：无法移入回收站"

        _record_change(abs_path, "delete", "delete_file", before_text, "")
        return (
            f"✅ 已删除（移入回收站）：{path}\n"
            f"回收站条目：{os.path.basename(trash_path)}（可在界面「回收站」里一键恢复）"
        )
    except Exception as e:
        return f"❌ 删除失败：{e}"


def write_file(path: str, content: str) -> str:
    """覆盖写入文件。会先备份原文件到 .trash 目录。"""
    path = _normalize_path(path)
    if not is_allowed_path(path):
        return f"❌ 拒绝访问：{path} 不在白名单内"

    try:
        abs_path = os.path.realpath(path)

        if not os.path.exists(abs_path):
            return f"❌ 文件不存在：{path}（新建文件请用 create_file）"

        if os.path.isdir(abs_path):
            return f"❌ 不能写入目录：{path}"

        if len(content) > MAX_FILE_SIZE:
            return f"❌ 内容太大（{len(content)} 字符），超过限制"

        # 先读一份改动前的内容（既给 diff 用，也顺手校验可读性）
        before_text = _read_text_safe(abs_path)

        # 先备份原文件到 .trash（带 sidecar，前端可一键恢复）
        backup_path = trash.backup_file(abs_path, kind="backup")

        # 写入新内容
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)

        _record_change(abs_path, "modify", "write_file", before_text, content)

        return (
            f"✅ 已写入文件：{path}（{len(content)} 字符）\n"
            f"原文件已备份到回收站："
            f"{os.path.basename(backup_path) if backup_path else '（备份失败）'}"
        )
    except Exception as e:
        return f"❌ 写入失败：{e}"


def edit_file(path: str, old_text: str, new_text: str) -> str:
    """
    精确替换文件内容（改大文件首选）。
    - old_text 必须唯一
    - 只替换 1 处
    """
    path = _normalize_path(path)
    if not is_allowed_path(path):
        return f"❌ 拒绝访问：{path} 不在白名单内"

    if not old_text:
        return "❌ old_text 为空"

    try:
        abs_path = os.path.realpath(path)
        if not os.path.isfile(abs_path):
            return f"❌ 不是文件：{path}"

        with open(abs_path, encoding="utf-8") as f:
            content = f.read()

        if old_text not in content:
            return "❌ 未找到要替换的内容（old_text 不匹配）"

        count = content.count(old_text)
        if count > 1:
            return f"❌ 找到 {count} 处匹配，请提供更精确的 old_text"

        new_content = content.replace(old_text, new_text, 1)

        # 备份（带 sidecar，前端可一键恢复）
        backup = trash.backup_file(abs_path, kind="edit")

        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(new_content)

        _record_change(abs_path, "modify", "edit_file", content, new_content)

        return (
            f"✅ 已替换 1 处（{len(old_text)} → {len(new_text)} 字符）\n"
            f"备份：{os.path.basename(backup) if backup else '（备份失败）'}"
        )
    except Exception as e:
        return f"❌ 编辑失败：{e}"


def _truncate(text: str, limit: int) -> str:
    if len(text) > limit:
        return text[:limit] + "\n\n...（输出被截断）"
    return text


def _host_run_python(code: str, timeout: int, cancel_event=None) -> str:
    """宿主机兜底执行 Python（仅在 AGENT_ALLOW_HOST_EXEC=1 时才会走到）。"""
    import sys
    import tempfile

    import runtime

    path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write(code)
            path = f.name
        out = _run_subprocess(
            [sys.executable, path],
            runtime.get_project_root(),
            timeout,
            cancel_event=cancel_event,
        )
        return sandbox.HOST_EXEC_NOTICE + out
    except Exception as e:
        return f"❌ 执行失败：{e}"
    finally:
        if path:
            try:
                os.unlink(path)
            except Exception:
                pass


def run_python(code: str, timeout: int = 30) -> str:
    """在 Docker 沙箱里执行 Python 代码。

    - code: Python 源码字符串
    - timeout: 超时秒数（默认 30）

    沙箱内的工作目录是**挂载进来的真实项目根目录**，与 read_file / write_file
    等文件工具看到的是同一份文件（早期实现挂的是 ~/ai_workspace，两者不一致）。
    """
    import runtime

    if len(code) > 50_000:
        return "❌ 代码太长（超过 50000 字符）"

    # 用 base64 编码后写进容器，避免 shell 转义问题
    import base64

    encoded = base64.b64encode(code.encode("utf-8")).decode("ascii")
    script = (
        f"echo {encoded} | base64 -d > /tmp/_agent_run.py && "
        f"cd /workspace && python /tmp/_agent_run.py"
    )

    res = sandbox.run_script(
        script, timeout, runtime.get_project_root(), cancel_event=runtime.get_cancel_event()
    )
    if not res.ran:
        # 只有用户显式设置 AGENT_ALLOW_HOST_EXEC=1 才可能走到这里
        return _host_run_python(code, timeout, cancel_event=runtime.get_cancel_event())
    return _truncate(res.output, 5000)


# ===== 工具权限分类（全项目唯一的事实来源）=====
# 权限分级与「要不要弹确认」都只查这张表。新增工具必须在这里登记，
# 否则完备性测试会失败——这是防止「加了工具忘了分类」导致权限被绕过的有效手段。
#
#   read     : 只读，任何等级都直接执行
#   internal : 只动会话内部状态（计划 / 记忆），不碰用户文件，任何等级都放行
#   mutate   : 会改磁盘（建 / 改 / 批量改 / 删）
#   execute  : 会执行代码或命令（含 git 写操作）
TOOL_PERMISSION = {
    # --- 只读 ---
    "read_file": "read",
    "read_file_range": "read",
    "list_dir": "read",
    "search_files": "read",
    "find_references": "read",
    "get_file_info": "read",
    "get_function_body": "read",
    "list_symbols": "read",
    "project_info": "read",
    "git_status": "read",
    "git_diff": "read",
    "git_log": "read",
    "search_knowledge": "read",
    # --- 会话内部状态 ---
    "create_plan": "internal",
    "update_plan": "internal",
    "get_plan": "internal",
    "save_memory": "internal",
    "list_memories": "internal",
    "delete_memory": "internal",
    # --- 改磁盘 ---
    "write_file": "mutate",
    "create_file": "mutate",
    "edit_file": "mutate",
    "multi_edit": "mutate",
    "delete_file": "mutate",
    # --- 执行 ---
    "run_command": "execute",
    "run_python": "execute",
    "run_tests": "execute",
    "git_commit": "execute",
    "git_rollback": "execute",
}

# 默认按最危险处理：万一有工具漏登记，宁可多要一次确认，也不能悄悄放行
DEFAULT_PERMISSION_KIND = "mutate"


def tool_permission_kind(name: str) -> str:
    # MCP 工具的危险档位由「用户对该 server 的标注」决定，默认 mutate（要确认）。
    # 注意：**不采信 server 自己声明的 readOnlyHint** —— 那是第三方自述，
    # 恶意 server 完全可以给破坏性工具标成只读来绕过确认。
    if isinstance(name, str) and name.startswith("mcp__"):
        try:
            import mcp_client
            return mcp_client.permission_kind(name)
        except Exception:                                       # noqa: BLE001
            return "mutate"
    return TOOL_PERMISSION.get(name, DEFAULT_PERMISSION_KIND)


def is_privileged_tool(name: str) -> bool:
    """是否需要「等级 ≥2」才允许执行。"""
    return tool_permission_kind(name) in ("mutate", "execute")


# ===== 危险操作黑名单（所有等级一律生效，等级 3 也拦）=====
# ⚠️ 这只是**减速带**，不是安全边界：关键词匹配永远能被绕过。
#    真正拦住 Agent 的是沙箱（--network=none、--cap-drop=ALL、非 root、
#    只挂载项目根目录）。这份清单挡住的是「明显不该做」的动作。
#    按用户要求：**禁止一切外发**（联网、推送、安装、发信）。
BANNED_RULES = [
    # --- 外发 / 联网 ---
    ("curl ", "联网下载"),
    ("wget ", "联网下载"),
    ("nc ", "网络连接"),
    ("ncat ", "网络连接"),
    ("netcat", "网络连接"),
    ("telnet", "远程登录"),
    ("ssh ", "远程登录"),
    ("scp ", "远程复制"),
    ("sftp", "远程复制"),
    ("rsync ", "远程同步"),
    ("ftp ", "网络传输"),
    ("git push", "推送到远端"),
    ("git remote add", "添加远端"),
    ("git remote set-url", "修改远端"),
    ("git fetch", "访问远端"),
    ("git pull", "访问远端"),
    ("git clone", "访问远端"),
    ("pip install", "联网安装（会执行 postinstall 脚本）"),
    ("pip3 install", "联网安装"),
    ("npm install", "联网安装（会执行 postinstall 脚本）"),
    ("yarn add", "联网安装"),
    ("apt install", "联网安装"),
    ("apt-get install", "联网安装"),
    ("snap install", "联网安装"),
    ("sendmail", "发送邮件"),
    # --- 递归强删（各种拼写）---
    ("rm -rf", "递归强制删除"),
    ("rm -fr", "递归强制删除"),
    ("rm -r -f", "递归强制删除"),
    ("rm -f -r", "递归强制删除"),
    ("rm --recursive", "递归删除"),
    ("rm --force", "强制删除"),
    ("find / -delete", "遍历删除"),
    ("shred ", "不可恢复擦除"),
    ("truncate -s 0", "清空文件"),
    ("mkfs", "格式化文件系统"),
    ("dd if=", "裸设备写入"),
    ("dd of=", "裸设备写入"),
    ("> /dev/", "写裸设备"),
    ("fdisk", "分区操作"),
    ("parted", "分区操作"),
    ("mount ", "挂载"),
    ("umount", "卸载"),
    # --- 提权 ---
    ("sudo ", "提权"),
    ("sudo\t", "提权"),
    ("su -", "切换用户"),
    ("su root", "切换用户"),
    ("pkexec", "提权"),
    ("doas ", "提权"),
    ("chmod 777", "放开全部权限"),
    ("chown ", "改属主"),
    # --- 系统控制 ---
    ("shutdown", "关机"),
    ("reboot", "重启"),
    ("init ", "切换运行级"),
    ("systemctl", "系统服务控制"),
    ("service ", "系统服务控制"),
    ("kill -9 -1", "杀全部进程"),
    ("pkill -9", "批量强杀"),
    ("killall", "批量杀进程"),
    ("crontab", "写定时任务"),
    (":(){:|:&};:", "fork 炸弹"),
    (":(){", "fork 炸弹"),
    # --- 全局配置 / 凭据 ---
    ("git config --global", "改全局 git 配置"),
    ("~/.ssh", "读写 SSH 凭据"),
    ("id_rsa", "SSH 私钥"),
    ("/etc/passwd", "系统账户文件"),
    ("/etc/shadow", "系统口令文件"),
]


def check_banned(cmd: str) -> str | None:
    """命中黑名单则返回拒绝原因，否则 None。"""
    low = (cmd or "").lower()
    for pattern, reason in BANNED_RULES:
        if pattern.lower() in low:
            return f"❌ 命令被拒绝（{reason}）：包含禁止的关键字 `{pattern}`"
    return None


def run_command(cmd: str, timeout: int = 15) -> str:
    """在 Docker 沙箱里执行 shell 命令。

    - 命令只在挂载的项目根目录（容器内 /workspace）下执行
    - 默认 15 秒超时
    - 危险命令被黑名单拦截
    - Docker 不可用时**拒绝执行**（除非显式设置 AGENT_ALLOW_HOST_EXEC=1）
    """
    import runtime

    if not cmd or not cmd.strip():
        return "❌ 命令为空"

    if len(cmd) > 5000:
        return "❌ 命令太长（超过 5000 字符）"

    # 黑名单检查（含「禁止一切外发」，等级 3 也照样拦）
    banned = check_banned(cmd)
    if banned:
        return banned

    script = f"cd /workspace && {cmd}"
    res = sandbox.run_script(script, timeout, runtime.get_project_root())
    if not res.ran:
        # 只有显式开启 AGENT_ALLOW_HOST_EXEC 才会走到这里
        return _host_run_shell(cmd, timeout, cancel_event=runtime.get_cancel_event())
    return _truncate(res.output, 3000)


def _host_run_shell(cmd: str, timeout: int, cancel_event=None) -> str:
    """宿主机兜底执行 shell 命令（仅在 AGENT_ALLOW_HOST_EXEC=1 时使用）。"""
    import runtime

    out = _run_subprocess(
        ["sh", "-c", cmd],
        runtime.get_project_root(),
        timeout,
        cancel_event=cancel_event,
    )
    return sandbox.HOST_EXEC_NOTICE + _truncate(out, 3000)


# 只读命令前缀（不需要确认，直接执行）
READ_SAFE_PREFIXES = [
    # 只读文件操作
    "ls",
    "cat",
    "pwd",
    "head",
    "tail",
    "wc",
    "grep",
    "find",
    "file",
    "which",
    "whoami",
    "date",
    "env",
    "printenv",
    # git 只读
    "git status",
    "git log",
    "git diff",
    "git branch",
    "git show",
    # 版本查询（只读）
    "python --version",
    "python3 --version",
    "pip list",
    "pip show",
    "pip freeze",
    # 系统信息
    "df",
    "du",
    "free",
    "uptime",
]

# 写类 / 执行类命令前缀（当前为空 → 一律弹勾叉；如需放行可在此追加）
WRITE_SAFE_PREFIXES = []


def is_safe_command(cmd: str) -> bool:
    """判断命令是否「安全」（不需要用户确认）

    分级策略：
    - 读类命令（ls / cat / git status / ...）→ 安全，直接执行
    - 写类 / 执行类命令（mkdir / cp / make / npm install / git commit / ...）→ 一律弹勾叉
    """
    cmd = cmd.strip().lower()
    if not cmd:
        return False

    # 含管道 / 重定向 / 命令连接符 → 永远视为危险
    if any(c in cmd for c in ["|", ">", "<", "&&", ";", "`", "$("]):
        return False

    # ✅ python xxx.py 安全；python -c 一律危险
    if cmd.startswith("python ") or cmd.startswith("python3 "):
        # 用 shlex 切分，判断是否有 -c 参数（覆盖 -c / -c"xxx" / -c'xxx' / -c\n 等所有形式）
        try:
            parts = shlex.split(cmd)
        except ValueError:
            # 切分失败（比如引号不闭合）视为危险
            return False
        # 检查 -c 作为独立参数出现（parts[0] 是 python/python3 时）
        if len(parts) >= 2 and parts[0] in ("python", "python3"):
            if "-c" in parts[1:]:
                return False
        # 兜底：也检查字符串里是否包含 " -c"
        if " -c" in cmd:
            return False
        return True

    # 只读命令 → 安全
    for prefix in READ_SAFE_PREFIXES:
        if cmd == prefix or cmd.startswith(prefix + " "):
            return True

    # 写类命令目前无放行项（WRITE_SAFE_PREFIXES 为空 → 全部弹勾叉）
    for prefix in WRITE_SAFE_PREFIXES:
        if cmd == prefix or cmd.startswith(prefix + " "):
            return True

    return False


# ============ 计划管理（按会话隔离 + 落库）============
# 计划原先只在内存里，服务一重启 Planner 的拆解结果就没了。这里写穿到 SQLite。
_plan_store = SessionStateStore("plan")


def _plan_key() -> str:
    import runtime

    return runtime.session_key()


def _plan_list():
    """取当前会话的计划列表（不存在时返回空列表，不写库）。"""
    plan = _plan_store.get(_plan_key())
    return plan if isinstance(plan, list) else []


def clear_plan(session_id=None):
    """清理某个会话的计划（删除会话时调用，内存与库都清）。"""
    import runtime

    _plan_store.pop(runtime.session_key(session_id))


def reset_plans():
    """清空所有会话的计划（测试用）。"""
    _plan_store.purge_all()


def create_plan(steps) -> str:
    """创建任务计划（归属当前会话）。容忍 steps 是字符串或列表。"""
    if not steps:
        return "❌ 步骤列表为空"

    # 容错 1：字符串 → 尝试解析
    if isinstance(steps, str):
        import json
        import re
        try:
            parsed = json.loads(steps)
            if isinstance(parsed, list):
                steps = parsed
            else:
                steps = [str(parsed)]
        except Exception:
            cleaned = steps.strip().strip("[]").strip()
            parts = []
            for line in cleaned.split("\n"):
                line = line.strip().strip(",").strip()
                line = re.sub(r'^["\']|["\']$', "", line)
                if line:
                    parts.append(line)
            steps = parts if parts else [steps]

    # 容错 2：确保是列表
    if not isinstance(steps, list):
        steps = [str(steps)]

    # 容错 3：清理空白
    steps = [str(s).strip() for s in steps if str(s).strip()]

    if not steps:
        return "❌ 解析后步骤列表为空"

    plan = [{"step": s, "status": "pending"} for s in steps]
    _plan_store.set(_plan_key(), plan)
    lines = ["📋 已创建计划："]
    for i, item in enumerate(plan, 1):
        lines.append(f"  {i}. [ ] {item['step']}")
    return "\n".join(lines)


def update_plan(step_index: int, status: str) -> str:
    """更新某步骤状态。status: 'done' / 'failed' / 'doing'"""
    plan = _plan_list()
    if not plan:
        return "❌ 还没有计划"
    if step_index < 1 or step_index > len(plan):
        return f"❌ 步骤号超出范围（1-{len(plan)}）"
    if status not in ("done", "failed", "doing"):
        return "❌ status 必须是 done / failed / doing"

    plan[step_index - 1]["status"] = status
    _plan_store.set(_plan_key(), plan)  # 写回，确保落库（列表是原地修改）

    lines = ["📋 更新后的计划："]
    for i, item in enumerate(plan, 1):
        i2 = {"done": "✓", "failed": "✗", "doing": "⏳", "pending": " "}[item["status"]]
        lines.append(f"  {i}. [{i2}] {item['step']}")
    return "\n".join(lines)


def get_plan() -> str:
    """查看当前计划"""
    plan = _plan_list()
    if not plan:
        return "（当前无计划）"

    lines = ["📋 当前计划："]
    for i, item in enumerate(plan, 1):
        i2 = {"done": "✓", "failed": "✗", "doing": "⏳", "pending": " "}[item["status"]]
        lines.append(f"  {i}. [{i2}] {item['step']}")
    return "\n".join(lines)


def get_plan_data():
    """给前端用的，返回当前会话的结构化计划数据"""
    return _plan_list()


def search_knowledge(query: str, top_k: int = 3) -> str:
    """在知识库中检索相关内容"""
    import runtime
    if not query or not query.strip():
        return "❌ 查询内容为空"

    try:
        # 第一次：高阈值
        results = rag.search_similar(query, top_k=top_k, min_score=0.4, api_key=runtime.get_effective_rag_api_key(), base_url=runtime.get_effective_rag_base_url())

        # 如果没结果，降低阈值重试
        if not results:
            results = rag.search_similar(query, top_k=top_k, min_score=0.15, api_key=runtime.get_effective_rag_api_key(), base_url=runtime.get_effective_rag_base_url())

        if not results:
            return f"未在知识库中找到与「{query}」相关的内容。知识库中可能没有这方面的信息。"

        lines = [f"📚 找到 {len(results)} 个相关片段：\n"]
        for i, r in enumerate(results, 1):
            lines.append(f"[{i}] 来自《{r['filename']}》（相似度 {r['score']}）：")
            lines.append(r["text"])
            lines.append("")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ 检索失败：{e}"


def save_memory(content: str) -> str:
    """保存一条长期记忆（跨会话）"""
    if not content or not content.strip():
        return "❌ 记忆内容为空"

    try:
        import db

        mid = db.save_memory(content.strip())
        return f"✅ 已保存记忆（ID: {mid}）：{content.strip()}"
    except Exception as e:
        return f"❌ 保存失败：{e}"


def list_memories() -> str:
    """列出所有长期记忆"""
    try:
        import db

        memories = db.get_all_memories()
        if not memories:
            return "（暂无长期记忆）"

        lines = ["🧠 当前长期记忆："]
        for m in memories:
            lines.append(f"  [{m['id']}] {m['content']}  ({m['created_at']})")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ 查询失败：{e}"


def delete_memory(memory_id: int) -> str:
    """删除一条长期记忆"""
    try:
        import db

        db.delete_memory(memory_id)
        return f"✅ 已删除记忆 ID {memory_id}"
    except Exception as e:
        return f"❌ 删除失败：{e}"


def project_info() -> str:
    """返回当前项目信息：类型、结构、关键文件。"""
    import os

    import runtime
    root = runtime.get_project_root()
    if not os.path.isdir(root):
        return f"❌ 项目目录不存在：{root}"

    # 识别项目类型
    project_type = "未知"
    markers = {
        "pyproject.toml": "Python (pyproject)",
        "requirements.txt": "Python (requirements)",
        "package.json": "Node.js",
        "Cargo.toml": "Rust",
        "go.mod": "Go",
        "pom.xml": "Java (Maven)",
        "build.gradle": "Java (Gradle)",
    }
    for f, t in markers.items():
        if os.path.exists(os.path.join(root, f)):
            project_type = t
            break

    # 列出顶层结构
    items = []
    try:
        for name in sorted(os.listdir(root))[:30]:
            if name.startswith('.'):
                continue
            full = os.path.join(root, name)
            if os.path.isdir(full):
                items.append(f"  📁 {name}/")
            else:
                size = os.path.getsize(full)
                items.append(f"  📄 {name} ({size} bytes)")
    except Exception as e:
        return f"❌ 列出目录失败：{e}"

    return (
        f"📦 当前项目\n"
        f"  路径：{root}\n"
        f"  类型：{project_type}\n"
        f"  顶层结构：\n" + "\n".join(items)
    )


def list_symbols(path: str) -> str:
    """列出 Python 文件的所有函数/类（用 AST）。"""
    import ast
    path = _normalize_path(path)
    if not is_allowed_path(path):
        return f"❌ 拒绝访问：{path}"
    try:
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except SyntaxError as e:
        return f"❌ 语法错误：{e}"
    except Exception as e:
        return f"❌ 解析失败：{e}"

    lines = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            args = [a.arg for a in node.args.args]
            lines.append(f"  L{node.lineno}: def {node.name}({', '.join(args)})")
        elif isinstance(node, ast.ClassDef):
            lines.append(f"  L{node.lineno}: class {node.name}")
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef):
                    args = [a.arg for a in sub.args.args]
                    lines.append(f"    L{sub.lineno}: def {sub.name}({', '.join(args)})")
    return f"📄 {path} 的符号：\n" + "\n".join(lines) if lines else "（无符号）"


def find_references(symbol: str, path: str = ".") -> str:
    """在项目里找某个符号的引用。"""
    import os
    import re
    path = _normalize_path(path)
    if not is_allowed_path(path):
        return f"❌ 拒绝访问：{path}"
    pattern = re.compile(r'\b' + re.escape(symbol) + r'\b')
    results = []
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('__pycache__', 'node_modules', '.venv')]
        for fname in files:
            if not fname.endswith('.py'):
                continue
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, encoding="utf-8", errors="ignore") as f:
                    for i, line in enumerate(f, 1):
                        if pattern.search(line):
                            rel = os.path.relpath(fpath, path)
                            results.append(f"{rel}:{i}: {line.strip()[:120]}")
                            if len(results) >= 50:
                                break
            except Exception:
                continue
    return f"'{symbol}' 的引用（{len(results)} 处）：\n" + "\n".join(results) if results else f"未找到 '{symbol}'"


def get_function_body(path: str, func_name: str) -> str:
    """读取某个函数的完整代码。"""
    import ast
    path = _normalize_path(path)
    if not is_allowed_path(path):
        return f"❌ 拒绝访问：{path}"
    try:
        with open(path, encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source)
    except Exception as e:
        return f"❌ 解析失败：{e}"
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            lines = source.splitlines()
            return f"函数 {func_name}（L{node.lineno}-L{node.end_lineno}）：\n" + "\n".join(lines[node.lineno-1:node.end_lineno])
    return f"❌ 未找到函数：{func_name}"


def multi_edit(edits: list) -> str:
    """一次改多个文件（真正的原子操作）。

    两阶段提交：
    1. 预检 + 内存替换：校验每个 edit 的参数、路径合法性、old_text 是否匹配且唯一，
       全部在内存中完成替换；任一项失败则整体放弃，**不落盘**。
    2. 统一落盘：先备份再写入；写入阶段若失败，用备份回滚已写文件。

    edits: [{"path": "...", "old_text": "...", "new_text": "..."}, ...]
    """
    import shutil

    if not isinstance(edits, list) or not edits:
        return "❌ edits 必须是非空列表"

    # ---------- 阶段 1：预检 + 内存替换 ----------
    pending = {}  # abs_path -> 累积替换后的内容
    for i, edit in enumerate(edits, 1):
        if not isinstance(edit, dict):
            return f"❌ multi_edit 已放弃（第 {i} 项格式错误）：{edit}"
        path = edit.get("path", "")
        old_text = edit.get("old_text", "")
        new_text = edit.get("new_text", "")
        if not path:
            return f"❌ multi_edit 已放弃（第 {i} 项 path 为空）"
        if not old_text:
            return f"❌ multi_edit 已放弃（第 {i} 项 old_text 为空）：{path}"

        abs_path = os.path.realpath(_normalize_path(path))
        if not is_allowed_path(abs_path):
            return f"❌ multi_edit 已放弃（第 {i} 项路径不在白名单）：{path}"
        if not os.path.isfile(abs_path):
            return f"❌ multi_edit 已放弃（第 {i} 项不是文件）：{path}"

        if abs_path not in pending:
            try:
                with open(abs_path, encoding="utf-8") as f:
                    pending[abs_path] = f.read()
            except Exception as e:
                return f"❌ multi_edit 已放弃（读取失败 {path}）：{e}"

        content = pending[abs_path]
        if old_text not in content:
            return f"❌ multi_edit 已放弃（第 {i} 项 old_text 不匹配）：{path}"
        count = content.count(old_text)
        if count > 1:
            return (
                f"❌ multi_edit 已放弃（第 {i} 项 old_text 在 {path} 中匹配 "
                f"{count} 处，不唯一）"
            )
        pending[abs_path] = content.replace(old_text, new_text, 1)

    # ---------- 阶段 2：统一落盘（带备份 + 失败回滚） ----------
    written = []  # [(backup_path, abs_path)]
    try:
        for abs_path, new_content in pending.items():
            backup = trash.backup_file(abs_path, kind="multiedit")
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(new_content)
            written.append((backup, abs_path))
            # 改动前的内容从备份里读 —— pending 在阶段 1 就已经被替换成新内容了，
            # 那里拿不到原文；而 backup 刚做出来，正好是改动前的快照。
            _record_change(abs_path, "modify", "multi_edit",
                           _read_text_safe(backup), new_content)
    except Exception as e:
        for backup, abs_path in written:
            if not backup:
                continue
            try:
                shutil.copy2(backup, abs_path)
            except Exception:
                pass
        return f"❌ multi_edit 写入失败，已回滚全部改动：{e}"

    lines = [f"📝 multi_edit 成功（{len(edits)} 处编辑 / {len(pending)} 个文件）："]
    for abs_path in pending:
        lines.append(f"  ✓ {abs_path}")
    lines.append("  每个文件的改动前版本都在回收站里，可在界面「回收站」一键恢复")
    return "\n".join(lines)


def _run_git_local(args: list) -> str:
    """执行 git（优先在 Docker 沙箱里，项目根目录挂载为 /workspace）。

    args 必须是**参数列表**：沙箱路径会把每个参数用 shlex.quote 转义后再拼成
    shell 脚本，因此 commit message / commit ref 里的引号、分号都不会被解释。

    git 属于受控命令，Docker 不可用时允许回退宿主机执行，但输出里会明确标注
    「未启用沙箱」，避免用户误以为仍在隔离环境里。
    """
    import runtime

    root = runtime.get_project_root()
    if not os.path.isdir(root):
        return f"❌ 项目目录不存在：{root}"

    res = sandbox.run_argv(
        ["git", *args],
        10,
        root,
        allow_host_fallback=True,
        cancel_event=runtime.get_cancel_event(),
    )
    if res.ran:
        return res.output

    # 回退：宿主机执行（参数列表，shell=False）
    return sandbox.HOST_EXEC_NOTICE + _run_subprocess(
        ["git", "-C", root, *args],
        root,
        10,
        cancel_event=runtime.get_cancel_event(),
    )


def _run_subprocess(cmd: list, cwd: str, timeout: int, cancel_event=None) -> str:
    """统一的子进程执行 + 输出拼装（shell=False，参数列表）。

    cancel_event 置位后**立刻** kill 子进程并返回，而不是等它自然跑完
    —— run_tests 最长 120 秒，让用户干等体验很差。
    输出重定向到临时文件而不是 PIPE，避免输出超过管道缓冲区时死锁。
    """
    import tempfile
    import time

    try:
        with tempfile.TemporaryFile("w+", encoding="utf-8") as fo, tempfile.TemporaryFile(
            "w+", encoding="utf-8"
        ) as fe:
            try:
                proc = subprocess.Popen(cmd, cwd=cwd, stdout=fo, stderr=fe, text=True)
            except FileNotFoundError:
                return f"❌ 命令不存在：{cmd[0]}"

            deadline = time.monotonic() + timeout
            cancelled = False
            timed_out = False
            while proc.poll() is None:
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    break
                if time.monotonic() > deadline:
                    timed_out = True
                    break
                time.sleep(0.1)

            if cancelled or timed_out:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=10)
                except Exception:
                    pass

            rc = proc.returncode
            fo.seek(0)
            fe.seek(0)
            out, err = fo.read(), fe.read()

        if cancelled:
            return "⏹ 已按用户请求中断执行" + (f"\n（中断前输出）\n{out[-500:]}" if out else "")
        if timed_out:
            return f"❌ 执行超时（>{timeout} 秒），已强制终止"

        text = ""
        if out:
            text += out
        if err:
            text += "\n【stderr】\n" + err
        text += f"\n【退出码】{rc}"
        return text
    except Exception as e:
        return f"❌ 执行失败：{e}"


def run_tests(path: str = "") -> str:
    """自动识别项目测试框架并跑测试。

    path: 可选，测试文件或目录路径。留空则自动识别。

    **默认在宿主机执行**：跑测试需要项目自身的虚拟环境与依赖，而沙箱镜像里
    只有通用依赖，用容器解释器跑项目测试往往直接失败。
    若希望测试也进沙箱，设置 ``AGENT_TESTS_IN_SANDBOX=1``（此时改用镜像自带的
    python，且容器默认无网络）。
    """
    import os

    import runtime

    root = runtime.get_project_root()
    if not os.path.isdir(root):
        return f"❌ 项目目录不存在：{root}"

    use_sandbox = sandbox.TESTS_IN_SANDBOX
    if use_sandbox:
        # 容器内跑：宿主机的 .venv 在容器里解释器是断的，只能用镜像自带的 python
        venv_python = "python3"
    else:
        venv_python = os.path.join(root, ".venv", "bin", "python")
        if not os.path.exists(venv_python):
            venv_python = "python3"  # fallback

    # 测试目标必须位于项目根之内，避免越界执行
    target = (path or "").strip()
    if target:
        root_real = os.path.realpath(root)
        cand = os.path.realpath(
            target if os.path.isabs(target) else os.path.join(root, target)
        )
        if not (cand == root_real or cand.startswith(root_real + os.sep)):
            return f"❌ 测试路径必须位于项目目录内：{path}"

    def _run(cmd: list) -> str:
        ev = runtime.get_cancel_event()
        if use_sandbox:
            res = sandbox.run_argv(cmd, 120, root, allow_host_fallback=True, cancel_event=ev)
            if res.ran:
                return res.output
            return sandbox.HOST_EXEC_NOTICE + _run_subprocess(
                cmd, root, 120, cancel_event=ev
            )
        return _run_subprocess(cmd, root, 120, cancel_event=ev)

    has_package_json = os.path.exists(os.path.join(root, "package.json"))
    has_cargo = os.path.exists(os.path.join(root, "Cargo.toml"))
    has_go = os.path.exists(os.path.join(root, "go.mod"))

    if has_package_json:
        out = _run(["npm", "test"])
    elif has_cargo:
        out = _run(["cargo", "test"])
    elif has_go:
        out = _run(["go", "test", "./..."])
    else:
        pytest_cmd = [venv_python, "-m", "pytest", "-v"]
        if target:
            pytest_cmd.append(target)
        out = _run(pytest_cmd)
        # pytest 失败 / 不可用时回退 unittest（与原 `||` 语义一致）
        if "【退出码】0" not in out:
            if target and target.endswith(".py"):
                # 指定了具体测试文件：直接按模块名运行，而不是全量 discover
                module = os.path.splitext(os.path.basename(target))[0]
                uni_cmd = [venv_python, "-m", "unittest", "-v", module]
            else:
                uni_cmd = [venv_python, "-m", "unittest", "discover", "-v", "-s", root]
            out += "\n\n【回退 unittest】\n" + _run(uni_cmd)

    if len(out) > 8000:
        out = out[:8000] + "\n\n...（输出被截断）"
    return out


def git_status() -> str:
    """查看 Git 状态。"""
    return _run_git_local(["status", "--short", "--branch"])


def git_diff() -> str:
    """查看 Git diff。"""
    return _run_git_local(["diff"])


def git_log(n: int = 10) -> str:
    """查看最近 n 条 commit。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "❌ n 必须是整数"
    n = max(1, min(n, 100))
    return _run_git_local(["log", "--oneline", "-n", str(n)])


def git_commit(message: str) -> str:
    """提交所有改动（危险：会弹勾叉）。"""
    if not message or not message.strip():
        return "❌ commit message 不能为空"
    r1 = _run_git_local(["add", "-A"])
    # 参数列表传递：message 中的引号 / 分号 / $() 都只是普通文本，不会被 shell 解释
    r2 = _run_git_local(["commit", "-m", message])
    return f"{r1}\n\n{r2}"


def git_rollback(commit: str = "HEAD~1") -> str:
    """回滚到某个 commit（危险：会弹勾叉）。"""
    import re

    commit = (commit or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_~^/.\-]+", commit):
        return f"❌ 非法的 commit 引用：{commit}"
    return _run_git_local(["reset", "--hard", commit])


# ============ Function Calling Schema ============

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "列出目录内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "目录路径，可以是绝对路径或相对路径。默认为当前目录。",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读读取文本文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径。"},
                    "max_lines": {
                        "type": "integer",
                        "description": "最多读取的行数，默认 500。",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "递归搜索文件内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "要搜索的关键词。"},
                    "path": {
                        "type": "string",
                        "description": "搜索起始目录，默认为当前目录。",
                    },
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_file_info",
            "description": "获取文件信息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件或目录路径。"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_file",
            "description": "创建新文件（存在会拒绝）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件路径，必须在白名单目录内。",
                    },
                    "content": {"type": "string", "description": "要写入的文件内容。"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "删除文件（移到回收站）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要删除的文件路径。"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "覆盖写入文件（会备份）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件路径，必须在白名单目录内。",
                    },
                    "content": {
                        "type": "string",
                        "description": "要写入的新内容（会完全替换原内容）。",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": "Docker 沙箱里执行 Python。",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "要执行的 Python 代码（多行用 \\n 分隔）。",
                    }
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Docker 沙箱里执行命令。",
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string", "description": "要执行的 shell 命令。"}
                },
                "required": ["cmd"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_plan",
            "description": "为复杂任务创建计划。",
            "parameters": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "任务步骤列表，每项是一个字符串描述。",
                    }
                },
                "required": ["steps"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_plan",
            "description": "更新计划步骤状态。",
            "parameters": {
                "type": "object",
                "properties": {
                    "step_index": {
                        "type": "integer",
                        "description": "步骤编号（从 1 开始）。",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["done", "failed", "doing"],
                        "description": "状态：done 完成 / failed 失败 / doing 进行中。",
                    },
                },
                "required": ["step_index", "status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_plan",
            "description": "查看当前计划。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": "在知识库中检索。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "查询关键词或问题。"},
                    "top_k": {
                        "type": "integer",
                        "description": "返回前 K 个最相关的片段，默认 3。",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "保存长期记忆。",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "要记住的内容。"}
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_memories",
            "description": "列出所有记忆。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_memory",
            "description": "删除记忆。",
            "parameters": {
                "type": "object",
                "properties": {
                    "memory_id": {"type": "integer", "description": "要删除的记忆 ID。"}
                },
                "required": ["memory_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "精确替换文件内容（改大文件首选）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径。"},
                    "old_text": {
                        "type": "string",
                        "description": "要替换的原文（必须唯一）。",
                    },
                    "new_text": {
                        "type": "string",
                        "description": "替换后的新文本。",
                    },
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file_range",
            "description": "读文件指定行范围（1-based）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径。"},
                    "start": {"type": "integer", "description": "起始行（默认 1）。"},
                    "end": {"type": "integer", "description": "结束行（默认 100）。"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "project_info",
            "description": "查看当前项目信息（路径、类型、顶层结构）。当用户问'当前项目''这是什么项目'时调用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_symbols",
            "description": "列出 Python 文件的所有函数/类（用 AST 解析）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Python 文件路径。"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_references",
            "description": "在项目里找某个符号（函数/类/变量）的引用位置。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "要查找的符号名。"},
                    "path": {"type": "string", "description": "搜索起始目录，默认当前项目。"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_function_body",
            "description": "读取某个函数的完整代码（只读该函数）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Python 文件路径。"},
                    "func_name": {"type": "string", "description": "函数名。"},
                },
                "required": ["path", "func_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "multi_edit",
            "description": "一次改多个文件（原子操作）。适合跨文件重构、批量替换。",
            "parameters": {
                "type": "object",
                "properties": {
                    "edits": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "old_text": {"type": "string"},
                                "new_text": {"type": "string"},
                            },
                            "required": ["path", "old_text", "new_text"],
                        },
                        "description": "编辑列表，每项包含 path / old_text / new_text。",
                    }
                },
                "required": ["edits"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": "查看当前项目的 Git 状态（改动文件、分支）。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": "查看 Git 改动 diff。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_log",
            "description": "查看最近 n 条 commit。",
            "parameters": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer", "description": "返回条数，默认 10。"}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_commit",
            "description": "提交所有改动（危险，会弹勾叉）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "commit message。"}
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_rollback",
            "description": "回滚到某个 commit（危险，会弹勾叉）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "commit": {"type": "string", "description": "目标 commit，默认 HEAD~1。"}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": "跑项目的测试（自动识别 pytest / unittest / jest / cargo / go test）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "可选，测试文件或目录路径。留空则自动识别。",
                    }
                },
            },
        },
    },
]
# 工具分组
CORE_TOOLS = [
    "list_dir",
    "read_file",
    "search_files",
    "create_file",
    "write_file",
    "edit_file",
    "project_info",
    "list_symbols",
    "find_references",
    "get_function_body",
    "run_python",
    "create_plan",
    "update_plan",
    "get_plan",
    "multi_edit",
    "git_status",
    "git_diff",
    "git_log",
    "git_commit",
    "git_rollback",
    "run_tests",
]

EXTENDED_TOOLS = [
    "get_file_info",
    "delete_file",
    "run_command",
    "search_knowledge",
    "save_memory",
    "list_memories",
    "delete_memory",
    "read_file_range",
]


# ===== MCP 插件工具（外部 server 提供）=====
# 注入是「按需」的：默认只有在任务里出现该 server 的名字/工具名/自定义关键词时才注入。
# 原因很实际 —— schema 是**每一步**都重发的，常驻注入等于按步数反复付钱。
def _mcp_tools_for_task(task_lower: str) -> list:
    try:
        import mcp_client
        import mcp_config
    except Exception:
        return []

    try:
        if not mcp_config.load_config().get("inject_enabled", True):
            return []          # 设置面板里的总开关关掉了
        schemas = mcp_client.tool_schemas()
    except Exception:
        return []

    if not schemas:
        return []

    # 按 server 分组，逐 server 判断要不要注入
    by_server = {}
    for sc in schemas:
        entry = mcp_client.resolve(sc["function"]["name"])
        if entry:
            by_server.setdefault(entry["server"], []).append(sc)

    picked = []
    for name, items in by_server.items():
        cfg = mcp_config.get_server(name) or {}
        if not cfg.get("enabled"):
            continue
        mode = cfg.get("inject") or "auto"
        if mode == "never":
            continue
        if mode == "always":
            picked.extend(items)
            continue
        # auto：server 名 / 工具名 / 自定义关键词命中才注入
        words = {name.lower()}
        words.update(str(k).lower() for k in (cfg.get("keywords") or []))
        for sc in items:
            fn = sc["function"]["name"]
            words.add(fn.lower())
            tool_part = fn.split("__")[-1] if "__" in fn else fn
            words.add(tool_part.lower().replace("_", " "))
        if any(w and w in task_lower for w in words):
            picked.extend(items)
    return picked


def get_tools_schema(task: str = "", full: bool = False):
    """
    根据任务选工具。
    - full=True：全发（多 Agent 复杂任务）
    - 默认：核心组 + 基础读工具 + 中文/英文关键词匹配 + 场景预置
    """
    if full:
        # 多 Agent 是「全发」，MCP 工具也要带上（仍受注入总开关与每 server 的注入模式约束）。
        # 之前这里直接 return，导致多 Agent 模式下 MCP 工具永远不可见。
        return TOOLS_SCHEMA + _mcp_tools_for_task((task or "").lower())

    # 默认核心组
    selected = set(CORE_TOOLS)

    # 基础集：常驻的读类工具
    selected.update(["list_dir", "read_file", "search_files", "get_file_info"])

    task_lower = task.lower()

    # ---- 中文关键词匹配 ----
    # 知识库（精确匹配）
    if any(k in task_lower for k in ["知识库", "文档", "上传", "资料"]):
        selected.add("search_knowledge")
    # 记忆
    if any(k in task_lower for k in ["记住", "偏好", "忘掉", "记忆"]):
        selected.update(["save_memory", "list_memories", "delete_memory"])
    # 删除（补齐：清理 / 清空 / 去掉）
    if any(k in task_lower for k in ["删除", "删掉", "移除", "清理", "清空", "去掉"]):
        selected.add("delete_file")
    # 命令 / 执行（去掉过宽的「执行」，补齐测试与构建词）
    if any(
        k in task_lower
        for k in [
            "命令",
            "git",
            "shell",
            "pytest",
            "测试",
            "跑一下",
            "运行脚本",
            "make",
            "npm",
        ]
    ):
        selected.add("run_command")
    # 文件信息（收紧：避免「信息 / 详情 / 大小」过宽触发）
    if any(
        k in task_lower
        for k in ["文件信息", "文件详情", "详细信息", "文件大小", "多大"]
    ):
        selected.add("get_file_info")
    # 分段读（补齐）
    if any(
        k in task_lower
        for k in ["大文件", "指定行", "行范围", "第几行", "局部", "分段读"]
    ):
        selected.add("read_file_range")

    # ---- 英文关键词匹配 ----
    if any(k in task_lower for k in ["knowledge", "doc", "document", "upload"]):
        selected.add("search_knowledge")
    if any(k in task_lower for k in ["run", "test", "shell", "command", "git"]):
        selected.add("run_command")
    if any(k in task_lower for k in ["delete", "remove", "clean"]):
        selected.add("delete_file")
    if any(k in task_lower for k in ["remember", "memory"]):
        selected.update(["save_memory", "list_memories"])
    if any(k in task_lower for k in ["read", "file", "line", "range"]):
        selected.add("read_file_range")

    # ---- 场景预置（Token 优化）----
    # 写作类
    if any(
        k in task_lower for k in ["写", "创建", "新建", "修改", "改", "编辑", "重构"]
    ):
        selected.update(["create_file", "write_file", "edit_file"])
    # 执行类
    if any(k in task_lower for k in ["运行", "执行", "测试", "run", "test"]):
        selected.update(["run_python", "run_command"])
    # 计划类（get_plan 已在 CORE，这里补齐 create / update）
    if any(k in task_lower for k in ["计划", "步骤", "规划"]):
        selected.update(["create_plan", "update_plan", "get_plan"])

    # 过滤
    builtin = [t for t in TOOLS_SCHEMA if t["function"]["name"] in selected]
    return builtin + _mcp_tools_for_task(task_lower)


# ============ 工具名 → 函数 映射 ============

TOOL_FUNCTIONS = {
    "list_dir": list_dir,
    "read_file": read_file,
    "search_files": search_files,
    "get_file_info": get_file_info,
    "create_file": create_file,
    "delete_file": delete_file,
    "write_file": write_file,
    "run_python": run_python,
    "run_command": run_command,
    "create_plan": create_plan,
    "update_plan": update_plan,
    "get_plan": get_plan,
    "search_knowledge": search_knowledge,
    "save_memory": save_memory,
    "list_memories": list_memories,
    "delete_memory": delete_memory,
    "edit_file": edit_file,
    "read_file_range": read_file_range,
    "project_info": project_info,
    "list_symbols": list_symbols,
    "find_references": find_references,
    "get_function_body": get_function_body,
    "multi_edit": multi_edit,
    "git_status": git_status,
    "git_diff": git_diff,
    "git_log": git_log,
    "git_commit": git_commit,
    "git_rollback": git_rollback,
    "run_tests": run_tests,
}
# 导出给 agent.py 用
__all__ = [
    # ... 原有 ...
    "is_safe_command",
]


# 供 agent.py 使用：判断一个工具是不是内置的（用于 MCP 分支前的短路）
def is_builtin_tool(name: str) -> bool:
    return name in TOOL_FUNCTIONS


def execute_tool(name: str, args: dict) -> str:
    """执行工具。**全项目唯一的工具派发入口** —— MCP 工具也从这里走，
    因此权限闸门、失败熔断、SSE 事件、前端执行详情全部自动复用。"""
    # MCP 插件工具：交给 MCP 客户端（异步桥接在 mcp_client 内部完成）
    if isinstance(name, str) and name.startswith("mcp__"):
        try:
            import mcp_client
            return mcp_client.call_tool(name, args or {})
        except Exception as e:                                  # noqa: BLE001
            return f"❌ MCP 工具调用失败：{e}"

    if name not in TOOL_FUNCTIONS:
        return f"❌ 未知工具：{name}"
    try:
        return TOOL_FUNCTIONS[name](**args)
    except Exception as e:
        return f"❌ 工具执行出错：{e}"
