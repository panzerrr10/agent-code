# 前端依赖（本地化，离线可用）

本项目宣称「可在本机私有部署」，因此前端第三方库**不再从 CDN 加载**，
而是全部落盘到本目录，由 Flask 的 `/static/vendor/...` 提供。

断网 / 内网 / 无外网的机器上，Markdown 渲染、代码高亮与 XSS 过滤均可正常工作。

| 文件 | 库 | 版本 | 许可证 | 来源 |
|---|---|---|---|---|
| `marked.min.js` | [marked](https://github.com/markedjs/marked) | 4.3.0 | MIT | `https://cdn.bootcdn.net/ajax/libs/marked/4.3.0/marked.min.js` |
| `purify.min.js` | [DOMPurify](https://github.com/cure53/DOMPurify) | 3.4.16 | Apache-2.0 或 MPL-2.0（双许可） | `https://cdn.jsdelivr.net/npm/dompurify@3/dist/purify.min.js` |
| `highlight.min.js` | [highlight.js](https://github.com/highlightjs/highlight.js) | 11.9.0 | BSD-3-Clause | `https://cdn.bootcdn.net/ajax/libs/highlight.js/11.9.0/highlight.min.js` |
| `github-dark.min.css` | highlight.js 主题（GitHub Dark） | 11.9.0 | BSD-3-Clause | `https://cdn.bootcdn.net/ajax/libs/highlight.js/11.9.0/styles/github-dark.min.css` |
| `codemirror.min.js` | [CodeMirror 6](https://codemirror.net/)（含 13 种语言模式） | 见下 | MIT | **本地构建**，见「构建 CodeMirror」一节 |

## 用途

- **marked**：把模型返回的 Markdown 渲染成 HTML。
- **DOMPurify**：对渲染结果做 XSS 清洗（`DOMPurify.sanitize(marked.parse(...))`）。
- **highlight.js**：代码块语法高亮。
- **CodeMirror 6**：右侧文件面板的代码编辑器（查看 / 编辑工作区里的文件）。

## 升级方式


## 构建 CodeMirror

CodeMirror 6 是 ESM 多包结构，不能像其它库那样直接下一个 UMD 文件用 `<script>` 引入，
因此用 esbuild 打成**单个自包含 IIFE**（挂到 `window.AgentCodeCM`）后再落盘 ——
打完即与 `node_modules` 无关，浏览器端零构建步骤。

```bash
./scripts/build-codemirror.sh          # 重新构建到本目录
./scripts/build-codemirror.sh --check  # 只检查 node/npm 是否齐备
```

入口源码在 [`scripts/codemirror-entry.js`](../../../scripts/codemirror-entry.js)，
它暴露的 API 只有 `create` / `detectLang` 两个函数，方便审计。

当前构建使用的版本（`@codemirror/*`）：

| 包 | 版本 | 包 | 版本 |
|---|---|---|---|
| `state` | 6.7.6 | `lang-python` | 6.2.1 |
| `view` | 6.43.14 | `lang-javascript` | 6.2.5 |
| `commands` | 6.11.1 | `lang-json` | 6.0.2 |
| `search` | 6.7.2 | `lang-html` | 6.4.12 |
| `autocomplete` | 6.20.3 | `lang-css` | 6.3.1 |
| `language` | 6.13.1 | `lang-markdown` | 6.5.2 |
| `lint` | 6.9.7 | `lang-xml` | 6.1.0 |
| `legacy-modes` | 6.5.5 | `lang-sql` | 6.10.0 |
| `theme-one-dark` | 6.1.3 | `lang-yaml` | 6.1.3 |

体积：703 KB（gzip 约 246 KB）。许可证：MIT。

```bash
cd static/vendor
curl -sSL -o marked.min.js       https://cdn.bootcdn.net/ajax/libs/marked/<版本>/marked.min.js
curl -sSL -o purify.min.js       https://cdn.jsdelivr.net/npm/dompurify@<版本>/dist/purify.min.js
curl -sSL -o highlight.min.js    https://cdn.bootcdn.net/ajax/libs/highlight.js/<版本>/highlight.min.js
curl -sSL -o github-dark.min.css https://cdn.bootcdn.net/ajax/libs/highlight.js/<版本>/styles/github-dark.min.css
```

升级后请同步更新上表的版本号，并跑一遍 `pytest`（有用例校验这些文件存在且页面不再引用外部域名）。

## 注意

各库均为 MIT / Apache-2.0 / BSD-3-Clause 等宽松许可证，随本项目分发无附加义务；
但请保留文件头部的版权与许可证注释（下载的原文件已自带，不要手动删除）。
