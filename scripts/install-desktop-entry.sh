#!/usr/bin/env bash
# 把 agent-code 安装成桌面应用（可固定到 GNOME Dock）。
#
# 安装三样东西（全部落在用户目录，不需要 sudo）：
#   1. ~/.local/bin/agent-code-app                    —— 启动器（窗口 + 服务管理）
#   2. ~/.local/share/applications/agent-code.desktop —— 桌面项（Dock / 应用菜单）
#   3. ~/.local/share/icons/hicolor/*/apps/agent-code.png —— 多尺寸图标
# 最后把它加入 GNOME 收藏夹（= 固定在左侧快捷栏）。
#
# 用法：
#   ./scripts/install-desktop-entry.sh              安装 / 重新安装（幂等）
#   ./scripts/install-desktop-entry.sh --uninstall  卸载（含从 Dock 移除）
#
# 环境变量：AGENT_CODE_PORT（默认 5004）

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(cd "$HERE/.." && pwd)"
PORT="${AGENT_CODE_PORT:-5004}"

APP_ID="agent-code"
APP_NAME="AI Agent"
SRC_LAUNCHER="$HERE/agent-code-app"
SRC_ICON="$PROJECT/web/icon.png"
PY="$PROJECT/.venv/bin/python"

BIN_DIR="$HOME/.local/bin"
APPS_DIR="$HOME/.local/share/applications"
ICON_ROOT="$HOME/.local/share/icons/hicolor"
LAUNCHER="$BIN_DIR/agent-code-app"
DESKTOP="$APPS_DIR/$APP_ID.desktop"

die() { printf '❌ %s\n' "$*" >&2; exit 1; }
info() { printf '  %s\n' "$*"; }

# ---------- 卸载 ----------
if [ "${1:-}" = "--uninstall" ]; then
    rm -f "$LAUNCHER" "$DESKTOP"
    for s in 48 64 128 256 512; do rm -f "$ICON_ROOT/${s}x${s}/apps/$APP_ID.png"; done
    info "已删除启动器、桌面项与图标"
    command -v gsettings >/dev/null 2>&1 && python3 - "$APP_ID.desktop" <<'PYEOF'
import ast, subprocess, sys
target = sys.argv[1]
out = subprocess.run(["gsettings", "get", "org.gnome.shell", "favorite-apps"],
                     capture_output=True, text=True).stdout.strip()
try:
    lst = ast.literal_eval(out)
except Exception:
    sys.exit(0)
if target in lst:
    lst = [x for x in lst if x != target]
    subprocess.run(["gsettings", "set", "org.gnome.shell", "favorite-apps",
                    "[" + ", ".join(f"'{x}'" for x in lst) + "]"], check=False)
    print("  已从 Dock 收藏夹移除")
PYEOF
    [ -x "$LAUNCHER" ] || info "如需彻底停掉后台服务：pkill -f 'src/app.py'"
    exit 0
fi

echo "▶ 安装 agent-code 桌面项"
echo "  项目路径: $PROJECT"

# ---------- 1. 启动器 ----------
[ -f "$SRC_LAUNCHER" ] || die "找不到启动器模板：$SRC_LAUNCHER"
mkdir -p "$BIN_DIR"
# 把模板里的默认项目路径替换成本机实际路径
sed "s|^PROJECT=.*|PROJECT=\"\${AGENT_CODE_PROJECT:-$PROJECT}\"|" \
    "$SRC_LAUNCHER" >"$LAUNCHER"
chmod +x "$LAUNCHER"
info "启动器  -> $LAUNCHER"

# ---------- 2. 图标 ----------
[ -f "$SRC_ICON" ] || die "找不到图标：$SRC_ICON"
if [ -x "$PY" ]; then
    "$PY" - "$SRC_ICON" "$ICON_ROOT" "$APP_ID" <<'PYEOF'
import os, sys
from PIL import Image

src, icon_root, app_id = sys.argv[1], sys.argv[2], sys.argv[3]
img = Image.open(src).convert("RGBA")
for size in (48, 64, 128, 256, 512):
    d = os.path.join(icon_root, f"{size}x{size}", "apps")
    os.makedirs(d, exist_ok=True)
    img.resize((size, size), Image.LANCZOS).save(
        os.path.join(d, f"{app_id}.png"), "PNG", optimize=True
    )
print(f"  图标    -> {icon_root}/{{48,64,128,256,512}}x.../apps/{app_id}.png")
PYEOF
else
    die "缺少虚拟环境 $PY，无法生成图标（先按 README 建好 .venv）"
fi

# ---------- 3. 桌面项 ----------
mkdir -p "$APPS_DIR"
cat >"$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=$APP_NAME
Name[zh_CN]=$APP_NAME
GenericName=agent-code
GenericName[zh_CN]=AI 编程 Agent
Comment=Local AI coding agent — read, edit, run and test code in one window
Comment[zh_CN]=本地 AI 编程 Agent：在独立窗口中读写代码、执行与测试
Exec=$LAUNCHER
Icon=$APP_ID
Terminal=false
Categories=Development;
Keywords=agent;ai;code;coding;deepseek;llm;agent-code;
StartupNotify=true
StartupWMClass=$APP_ID
EOF
info "桌面项  -> $DESKTOP"

command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database "$APPS_DIR" >/dev/null 2>&1 && info "已刷新桌面数据库"
command -v gtk-update-icon-cache >/dev/null 2>&1 && \
    gtk-update-icon-cache -f -t "$ICON_ROOT" >/dev/null 2>&1 && info "已刷新图标缓存"

# ---------- 4. 固定到 GNOME Dock ----------
if command -v gsettings >/dev/null 2>&1 && gsettings get org.gnome.shell favorite-apps >/dev/null 2>&1; then
    python3 - "$APP_ID.desktop" <<'PYEOF'
import ast, subprocess, sys
target = sys.argv[1]
out = subprocess.run(["gsettings", "get", "org.gnome.shell", "favorite-apps"],
                     capture_output=True, text=True).stdout.strip()
lst = ast.literal_eval(out)
if target in lst:
    print("  Dock    -> 已在收藏夹中（未改动）")
else:
    lst.append(target)
    subprocess.run(["gsettings", "set", "org.gnome.shell", "favorite-apps",
                    "[" + ", ".join(f"'{x}'" for x in lst) + "]"], check=True)
    print("  Dock    -> 已固定在左侧快捷栏")
PYEOF
else
    info "Dock     -> 当前会话没有 GNOME（跳过固定），可从应用菜单启动"
fi

echo
echo "✅ 安装完成。点击左侧快捷栏的「$APP_NAME」图标即可启动（首次冷启动约 3~8 秒）。"
echo "   命令行也可用：agent-code-app  |  --status  |  --stop  |  --log"
