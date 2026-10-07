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

## 用途

- **marked**：把模型返回的 Markdown 渲染成 HTML。
- **DOMPurify**：对渲染结果做 XSS 清洗（`DOMPurify.sanitize(marked.parse(...))`）。
- **highlight.js**：代码块语法高亮。

## 升级方式

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
