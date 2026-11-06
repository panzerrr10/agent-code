#!/usr/bin/env bash
# 把 docs/演示脚本.md 转成 PDF（和 agent-code介绍.pdf 同一套排版）
# 用法：bash scripts/演示脚本转pdf.sh [输出路径]
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="${1:-$HOME/agent-code演示脚本.pdf}"
node scripts/md2html.js docs/演示脚本.md /tmp/demo_script.html
google-chrome --headless=new --disable-gpu --no-sandbox --no-pdf-header-footer \
  --print-to-pdf="$OUT" --virtual-time-budget=8000 file:///tmp/demo_script.html
echo "已生成：$OUT"
