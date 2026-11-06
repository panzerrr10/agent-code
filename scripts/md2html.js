const fs = require('fs');
const path = require('path');
const mdPath = process.argv[2];
const outPath = process.argv[3];
const mode = process.argv[4] || 'demo';          // 'demo' | 'readme'
const baseDir = path.resolve(path.dirname(mdPath));

// 直接 require 项目自带的 marked（UMD/CommonJS 兼容，离线可用）
const marked = require(path.resolve('web/static/vendor/marked.min.js'));
if (!marked || typeof marked.parse !== 'function') {
  console.error('marked 加载失败：' + JSON.stringify(Object.keys(marked || {})));
  process.exit(1);
}

let md = fs.readFileSync(mdPath, 'utf8');
// 去掉文档自己的第一行 H1 —— 页眉里已经有标题了，重复会显得冗余
md = md.replace(/^#\s+.*\n/, '');

// readme 模式：开头那段 blockquote 是项目一句话简介，移到页眉去，正文不再重复
let tagline = '';
if (mode === 'readme') {
  const m = md.match(/^\s*>\s*([\s\S]*?)(?=\n\s*\n)/);
  if (m) {
    tagline = marked.parseInline(
      m[1].split('\n').map(l => l.replace(/^\s*>\s?/, '')).join(' ')
    );
    md = md.slice(0, m.index) + md.slice(m.index + m[0].length);
  }
}
let body = marked.parse(md, { gfm: true, breaks: false });

// 相对图片路径 → file:// 绝对路径（否则 HTML 放在 /tmp 时图片全丢）
body = body.replace(/<img([^>]*?)src="([^"]+)"/g, (all, pre, src) => {
  if (/^(https?:|data:|file:)/.test(src)) return all;
  const abs = path.resolve(baseDir, src);
  return '<img' + pre + 'src="file://' + abs + '"';
});

const css = `
  @page { size: A4; margin: 16mm 15mm 15mm 15mm;
          @bottom-center { content: counter(page); } }
  * { box-sizing: border-box; }
  body { font-family: "Noto Sans CJK SC", "Noto Sans CJK", sans-serif;
         font-size: 10.1pt; line-height: 1.55; color: #1a1f28; margin: 0; }

  .doc-head { margin-bottom: 16px; padding-bottom: 12px; border-bottom: 2px solid #eef0f4; }
  .doc-head .kicker { font-size: 9pt; letter-spacing: 2.5px; color: #4f46e5; font-weight: 700; }
  .doc-head h1 { font-size: 21pt; margin: 6px 0 5px; letter-spacing: -0.3px;
                 border: none; padding: 0; }
  .doc-head .meta { font-size: 9.2pt; color: #6b7280; line-height: 1.8; }
  .doc-head .meta b { color: #1a1f28; }

  h1 { font-size: 15pt; margin: 19px 0 9px; padding-bottom: 6px;
       border-bottom: 2px solid #eef0f4; page-break-after: avoid; }
  h2 { font-size: 12pt; margin: 15px 0 7px; color: #1f2937; page-break-after: avoid; }
  h3 { font-size: 10.8pt; margin: 14px 0 6px; color: #374151; page-break-after: avoid; }

  p { margin: 0 0 8px; }
  ul, ol { margin: 5px 0 10px; padding-left: 20px; }
  li { margin-bottom: 3px; }
  li > ul, li > ol { margin: 3px 0 5px; }

  code { font-family: "DejaVu Sans Mono", monospace; font-size: 8.7pt;
         background: #f1f3f7; padding: 1px 4px; border-radius: 3px; color: #b1286b; }
  pre { font-family: "DejaVu Sans Mono", monospace; font-size: 8.2pt; line-height: 1.45;
        background: #1e2430; color: #dfe4ec; padding: 9px 12px; border-radius: 5px;
        overflow-x: auto; margin: 7px 0 11px; page-break-inside: avoid;
        white-space: pre-wrap; word-break: break-word; }
  pre code { background: none; color: inherit; padding: 0; font-size: inherit; }

  table { width: 100%; border-collapse: collapse; margin: 7px 0 12px;
          font-size: 9.1pt; page-break-inside: auto; }
  tr { page-break-inside: avoid; }
  thead { display: table-header-group; }
  th { background: #f5f6fa; text-align: left; padding: 5px 8px;
       border-bottom: 1.5px solid #d9dee6; font-weight: 600; color: #374151; }
  td { padding: 4.5px 8px; border-bottom: 1px solid #eef0f4; vertical-align: top; }

  blockquote { margin: 7px 0 10px; padding: 8px 12px; background: #eef2ff;
               border-left: 3px solid #4f46e5; border-radius: 0 4px 4px 0;
               font-size: 9.5pt; page-break-inside: avoid; }
  blockquote p:last-child { margin-bottom: 0; }

  hr { border: none; border-top: 1px solid #eef0f4; margin: 18px 0; }

  .task-list-item { list-style: none; margin-left: -18px; }
  input[type=checkbox] { margin-right: 6px; vertical-align: middle; }

  /* 截图：全宽、描边、不跨页 */
  img { max-width: 100%; height: auto; display: block; margin: 10px 0 14px;
        border: 1px solid #e5e7eb; border-radius: 6px;
        box-shadow: 0 2px 10px rgba(16,24,40,.07); page-break-inside: avoid; }

  em { color: #4b5563; }
  a { color: #4f46e5; text-decoration: none; }
  h1 + table, h1 + pre, h2 + table, h2 + pre, h3 + table { page-break-before: avoid; }
`;

const HEADERS = {
  demo: `
  <div class="kicker">LIVE DEMO SCRIPT</div>
  <h1>agent-code 现场演示脚本</h1>
  <div class="meta"><b>12~15 分钟</b> · 配套文档 <b>agent-code介绍.pdf</b>（技术细节） · 建议打印随身带</div>`,
  readme: `
  <div class="kicker">PROJECT OVERVIEW</div>
  <h1>agent-code</h1>
  <div class="sub">${tagline}</div>
  <div class="meta"><b>Python 3.12</b> · Flask · SSE · OpenAI SDK (Function Calling) · SQLite · ChromaDB · Docker · MCP<br>
  项目源码：github.com（私下沟通） · 详细技术手册见 <b>agent-code介绍.pdf</b></div>`,
};

const subCss = mode === 'readme' ? `
  .doc-head { margin-bottom: 14px; padding-bottom: 14px; border-bottom: 2px solid #eef0f4; }
  .doc-head .kicker { font-size: 9pt; letter-spacing: 2.5px; color: #4f46e5; font-weight: 700; }
  .doc-head h1 { font-size: 30pt; margin: 8px 0 6px; letter-spacing: -0.8px;
                 border: none; padding: 0; line-height: 1.15; }
  .doc-head .sub { font-size: 11.5pt; color: #374151; line-height: 1.7; margin-bottom: 10px; }
  .doc-head .meta { font-size: 9pt; color: #6b7280; line-height: 1.9; }
  .doc-head .meta b { color: #1f2937; }
` : '';

const html = `<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<title>${mode === 'readme' ? 'agent-code 项目介绍' : 'agent-code 演示脚本'}</title>
<style>${css}${subCss}</style></head><body>
<div class="doc-head">${HEADERS[mode] || HEADERS.demo}
</div>
${body}
</body></html>`;

fs.writeFileSync(outPath, html, 'utf8');
console.log('✓ HTML 已生成（' + html.length + ' 字节）');
