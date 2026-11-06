#!/usr/bin/env bash
# 把 README-项目介绍.md 转成 PDF（给面试官看的那份）
# 用法：bash scripts/项目介绍转pdf.sh [输出路径]
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="${1:-$HOME/agent-code项目介绍.pdf}"
node scripts/md2html.js README-项目介绍.md /tmp/readme.html readme
google-chrome --headless=new --disable-gpu --no-sandbox --no-pdf-header-footer \
  --print-to-pdf="$OUT" --virtual-time-budget=10000 file:///tmp/readme.html
echo "已生成：$OUT"
