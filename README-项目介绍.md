# agent-code

> 一个可**在本机私有部署**的 AI 编程 Agent。
> 用自然语言下指令，它自己决定调用哪些工具——读文件、改代码、跑测试、提交 Git——
> 在 Docker 沙箱里执行，全程流式回传进度。

![界面截图](docs/images/screenshot-light.png)

---

## 这个项目解决什么问题

大模型本身**不能执行任何东西**，它只会输出文字。

现有产品（ChatGPT、各类 Copilot）大多停留在"给你一段代码，你自己复制过去"。而真正完成开发任务需要的是：
**看得到你的项目、改得动你的文件、跑得了你的测试、并且出了问题能自己发现。**

agent-code 做的事就是把这个闭环补上：

```
用户下指令
    ↓
模型判断「需要先看看这个文件」→ 返回结构化工具调用（不是回答）
    ↓
程序真正执行 read_file → 拿到内容 → 回填给模型
    ↓
模型基于真实内容继续判断 → 改代码 / 跑测试 / 提交
    ↓
循环直到任务完成（上限 50 步）
```

**关键设计：模型只负责决策，执行永远在受控的代码里。** 这不是一个"聊天机器人"，而是一个能动手的执行体。

---

## 核心特性

| 特性 | 说明 |
|---|---|
| 🤖 **Function Calling 多步循环** | 模型决策 → 工具执行 → 结果回填 → 继续推理，上限 50 步防死循环 |
| ⚡ **真流式输出** | 后端按 50ms 节流边收边发，首字延迟 1.2 秒（改造前需等整段生成完毕，约 7 秒） |
| 🛡️ **三级权限 + 一次性令牌** | 只读 / 确认 / 全权；危险操作挂起等确认，令牌用后即失效（防伪造、防重放） |
| 📦 **Docker 沙箱执行** | 每次执行起临时容器：断网、限内存/CPU/进程数、只挂载当前项目目录 |
| 📝 **第 N 轮改动 + 只读 diff** | 每轮结束列出改了哪些文件（+n −n），点开看本轮 diff；与"打开文件"完全隔离，同一个文件可同时开两个标签 |
| 📉 **三层上下文压缩** | 32K token 预算，超了依次做工具结果剪枝 / 整段折叠 / 兜底截断；压缩后强制校验 tool_call 配对 |
| ⏹️ **断点续跑** | 每步状态写穿 SQLite，中断或重启后可从原步骤继续，不用从头重来 |
| 🧪 **评估集回归** | 17 条真实任务的基线，改完之后能看出通过率 / 平均步数 / token 是升是降 |
| 🔌 **MCP 插件** | 接入任意 MCP server（stdio / SSE / HTTP），走同一道权限闸门，可关进沙箱 |
| 🔑 **模型提供商完全自定义** | 代码中无预设厂商；界面填入 Base URL、API Key、模型名即可接入；密钥只进不出，接口只回掩码 |

---

## 架构

```
┌─ 入口层 ──────────────────────────────────────────┐
│  app.py (Web)    cli.py (命令行)   desktop.py (桌面)  │
└────────────────────────┬─────────────────────────┘
┌─ 编排层 ───────────────┴─────────────────────────┐
│  agent.py (主循环)    planner.py / reviewer.py       │
└────────────────────────┬─────────────────────────┘
┌─ 决策层 ───────────────┴─────────────────────────┐
│  policy.py (权限)   compaction.py (上下文)           │
│  runtime.py (请求隔离)   state_store.py (状态)       │
└────────────────────────┬─────────────────────────┘
┌─ 能力层 ───────────────┴─────────────────────────┐
│  tools.py (29 工具)   repomap.py (代码地图)          │
│  rag.py (知识库)      sandbox.py (沙箱)              │
└────────────────────────┬─────────────────────────┘
┌─ 数据层 ───────────────┴─────────────────────────┐
│  db.py (SQLite WAL)   trash.py (备份/回收站)         │
└────────────────────────┬─────────────────────────┘
┌─ 配置层 ───────────────┴─────────────────────────┐
│  config.py   config_providers.py   paths.py         │
└──────────────────────────────────────────────────┘
```

分层原则：**每一层只因为一个原因变化**。加工具只动能力层，换模型厂商只动配置层，主循环不受影响。

---

## 接口

项目有两类接口：

**1. HTTP API（52 条路由）** — 给前端与脚本调用

- `POST /run` — 单 Agent 运行，返回 **SSE 事件流**
- `POST /run_multi` — 多 Agent 三角色运行
- `POST /run/cancel` — 协作式中断
- 会话 / 工作区 / 文件 / 改动记录 / 知识库 / 插件 / 模型配置 各有对应 CRUD

长任务不是"请求 → 等待 → 响应"，而是**一个不结束的 HTTP 响应**：

```
data: {"type":"tool_call",    "name":"read_file", "args":{"path":"src/calc.py"}}
data: {"type":"tool_result",  "content":"def add(a, b):\n    return a - b"}
data: {"type":"text",         "content":"我看了 src/calc.py，"}
data: {"type":"token_usage",  "total_tokens":9432, "steps":5}
data: {"type":"done"}
```

**2. Agent 工具接口（29 个）** — 给大模型通过 Function Calling 调用

| 档位 | 数量 | 工具 |
|---|---|---|
| 只读 | 13 | `read_file` `list_dir` `search_files` `find_references` `list_symbols` `git_diff` … |
| 会话内部 | 6 | `create_plan` `update_plan` `save_memory` … |
| 改磁盘 | 5 | `write_file` `create_file` `edit_file` `multi_edit` `delete_file` |
| 执行 | 5 | `run_command` `run_python` `run_tests` `git_commit` `git_rollback` |

所有请求要求 `X-Auth-Token` 请求头（页面渲染时注入）。浏览器跨域请求无法设置自定义头，因此天然阻断 CSRF。

---

## 技术栈

| 层 | 技术 |
|---|---|
| 语言 | Python 3.12 |
| Web | Flask 3、SSE（`text/event-stream`） |
| 大模型 | OpenAI SDK（Function Calling、流式 delta 解析） |
| 并发隔离 | `contextvars`（每请求独立上下文，避免并发密钥串味） |
| 存储 | SQLite（WAL 模式、索引、写穿状态） |
| 向量检索 | ChromaDB + 厂商 embedding API |
| 执行隔离 | Docker（`--network=none`、`--memory`、`--cpus`、`--pids-limit`） |
| 代码分析 | Python `ast` + 多语言轻量解析（repo map） |
| 插件协议 | MCP（stdio / SSE / streamable-http） |
| 前端 | 原生 HTML/CSS/JS（无框架，单文件）+ CodeMirror 6 |
| 桌面 | pywebview |
| 测试 / CI | pytest（803 条）、ruff、GitHub Actions |

---

## 运行

```bash
# 1. 装依赖
pip install -r requirements.txt

# 2. 构建执行沙箱镜像（可选；不建则无法执行代码）
docker build -t agent-sandbox .

# 3. 启动
python src/app.py
# 打开 http://127.0.0.1:5004
```

首次使用需要在 **设置 → 模型与密钥** 里添加一个模型提供商（Base URL + API Key + 模型名），
内置「测试连接」会真发一次请求验证。

也可以跑桌面窗口：`python src/desktop.py` ／ 命令行：`python src/cli.py`

---

## 工程质量

| 项 | 现状 |
|---|---|
| 测试 | **803 条** pytest 用例 / 10,081 行，覆盖权限模型、安全边界、上下文压缩、多 Agent、前端静态检查 |
| CI | GitHub Actions：`ruff check` + `pytest`，不过不允许合并 |
| 静态检查 | ruff 零告警 |
| 评估集 | 17 条真实任务基线（通过率 / 平均步数 / token），支持改动前后对比 |
| 代码规模 | 27 个模块 / 13,080 行业务代码 + 8,844 行前端 |

**测试设计里比较有意思的两点：**

- **守卫测试**：不测功能，测"防止以后改坏"。例如「每个工具都必须登记权限档位，漏登记默认按最危险处理」「SSE 消费点只能有一处」。写完守卫后会**故意植入 bug 验证它真的会报错**——不会报警的守卫等于没有。
- **假模型测试**：Agent 行为不确定，所以用「假 LLM」返回固定响应，让主循环、工具派发、压缩逻辑可以确定性测试，且不花一分钱 token。

---

## 目录结构

```
src/            后端（27 个模块）
  agent.py        Agent 主循环
  tools.py        29 个工具 + 单一派发点
  compaction.py   三层上下文压缩
  sandbox.py      Docker 沙箱
  rag.py          知识库检索
  repomap.py      AST 代码地图
  db.py           SQLite 持久化
  app.py          Flask 路由（52 条）
web/
  templates/index.html   前端（单文件）
  static/vendor/         vendored 依赖（支持离线）
eval/           评估集（17 条任务 + 基线）
scripts/        构建脚本
docs/           文档与截图
```

---

## 已知限制

诚实说明，避免夸大：

- **单机部署**：用 SQLite 而非 MySQL/Postgres，没有多实例、高并发场景的验证
- **模型依赖**：效果强依赖所选模型的能力，弱模型在复杂任务上容易绕圈
- **评估集偏小**：17 条任务只能做回归网，样本量不足以做精细的 A/B 对比
- **多 Agent 是固定流水线**：Planner → Executor → Reviewer，没有动态编排
- **没有生产环境运维经验**：这是个人项目，没有真实流量下的调优与故障处理

---

## 文档

- [项目介绍（本文件）](README-项目介绍.md)
- 开发者文档：[README.md](README.md)
