#!/usr/bin/env bash
# 重新构建本地化的 CodeMirror 6 单文件包（web/static/vendor/codemirror.min.js）。
#
# 为什么需要构建：CodeMirror 6 是 ESM 多包结构，不能像 marked / DOMPurify 那样
# 直接下一个 UMD 文件用 <script> 引入。这里用 esbuild 把所有需要的包打成一个
# 自包含的 IIFE，挂到 window.AgentCodeCM —— 打完就与 node_modules 无关，
# 浏览器端零构建步骤，符合本项目「完全离线、不用 CDN」的原则。
#
# 用法：
#   ./scripts/build-codemirror.sh          # 构建到 web/static/vendor/
#   ./scripts/build-codemirror.sh --check   # 只检查工具链是否齐备
#
# 依赖：node + npm（仅构建时需要；日常运行前端不需要 node）

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(cd "$HERE/.." && pwd)"
ENTRY="$HERE/codemirror-entry.js"
OUT="$PROJECT/web/static/vendor/codemirror.min.js"
WORK="${CM_BUILD_DIR:-$(mktemp -d)}"

# 与已 vendored 版本保持一致；升级时改这里并重跑
PKGS=(
  "@codemirror/state@6" "@codemirror/view@6" "@codemirror/commands@6"
  "@codemirror/search@6" "@codemirror/autocomplete@6" "@codemirror/language@6"
  "@codemirror/lint@6"
  "@codemirror/lang-python@6" "@codemirror/lang-javascript@6" "@codemirror/lang-json@6"
  "@codemirror/lang-html@6" "@codemirror/lang-css@6" "@codemirror/lang-markdown@6"
  "@codemirror/lang-xml@6" "@codemirror/lang-sql@6" "@codemirror/lang-yaml@6"
  "@codemirror/legacy-modes@6" "@codemirror/theme-one-dark@6"
)

if [ "${1:-}" = "--check" ]; then
    command -v node >/dev/null || { echo "❌ 缺 node"; exit 1; }
    command -v npm >/dev/null || { echo "❌ 缺 npm"; exit 1; }
    echo "✅ node $(node --version) / npm $(npm --version)"
    exit 0
fi

command -v node >/dev/null || { echo "❌ 需要 node 才能构建 CodeMirror"; exit 1; }
command -v npm  >/dev/null || { echo "❌ 需要 npm 才能构建 CodeMirror"; exit 1; }
[ -f "$ENTRY" ] || { echo "❌ 找不到入口文件：$ENTRY"; exit 1; }

echo "▶ 构建 CodeMirror 6（工作目录 $WORK）"
cd "$WORK"
printf '{ "name": "cm-bundle", "private": true, "type": "module" }\n' > package.json

echo "  安装依赖…"
npm install --silent --no-audit --no-fund "${PKGS[@]}"

# 入口必须复制进构建目录再打包：esbuild 是按「入口文件所在目录」解析 import 的，
# 直接指向 scripts/ 里的源码会因为找不到 node_modules 而报 Could not resolve。
cp "$ENTRY" "$WORK/entry.js"

echo "  打包…"
npx --yes esbuild "$WORK/entry.js" \
    --bundle --minify --format=iife --target=es2020 \
    --legal-comments=none --outfile="$OUT"

echo "  ✅ 输出：$OUT（$(du -h "$OUT" | cut -f1)，gzip $(gzip -c "$OUT" | wc -c | awk '{printf "%d KB", $1/1024}')）"
echo
echo "  本次使用的版本（请同步更新 web/static/vendor/README.md）："
node -e '
const fs = require("fs");
for (const d of fs.readdirSync("node_modules/@codemirror").sort()) {
  const p = "node_modules/@codemirror/" + d + "/package.json";
  if (fs.existsSync(p)) console.log("    @codemirror/" + d + " " + JSON.parse(fs.readFileSync(p)).version);
}'

if [ -z "${CM_BUILD_DIR:-}" ]; then rm -rf "$WORK"; fi
