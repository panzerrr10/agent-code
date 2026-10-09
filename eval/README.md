# 评估集

## 为什么需要它

项目有 468 个单元测试，它们能证明**代码没坏**，但证明不了**改完到底变好还是变差**。

改一句系统提示词、换一个工具描述、调一次上下文压缩阈值 —— 这些改动的影响，
单元测试**一个都测不出来**。只能靠跑真实任务量。

有了这个，流程就变成：

```bash
# 1. 改之前，先存一份基线（只做一次）
python eval/run.py --all --out eval/results/baseline.json

# 2. 改代码

# 3. 改完对比
python eval/run.py --all --baseline eval/results/baseline.json
```

输出会告诉你：通过率是升是降、平均步数多了还是少了、token 省了还是涨了。

---

## 用法

```bash
# 列出所有任务
python eval/run.py --list

# 跑全部
python eval/run.py --all --out eval/results/xxx.json

# 只跑某几个 / 某个标签
python eval/run.py --tasks fix_failing_test,implement_function
python eval/run.py --tags bugfix

# 换模型对比（同一个 provider 下的不同模型）
python eval/run.py --all --model deepseek-reasoner --out eval/results/reasoner.json

# 与基线对比（改完之后最常用）
python eval/run.py --all --baseline eval/results/baseline.json

# 保留任务工作目录，便于复盘「它到底改了啥」
python eval/run.py --tasks fix_failing_test --keep -v
```

---

## 任务集（10 条）

| id | 考什么 | 标签 |
|---|---|---|
| `fix_failing_test` | 跑测试、从失败信息定位 bug | bugfix, test-driven |
| `fix_off_by_one` | 越界错误的定位与修复 | bugfix, debugging |
| `fix_import_error` | 导入名不匹配 | bugfix, imports |
| `implement_function` | 按文档字符串 + 测试实现函数 | feature, spec-following |
| `add_validation` | 补参数校验并满足异常类型要求 | feature, validation |
| `multi_file_rename` | 跨文件重命名 + 不留下旧名 | refactor, multi-file |
| `extract_function` | 统一两处不一致的实现并提取共用函数 | refactor, consistency |
| `find_bug_in_large_file` | 在稍大的文件里定位错误 | bugfix, navigation |
| `fix_three_bugs` | 一次修三个独立 bug（考坚持） | bugfix, persistence |
| `keep_public_api` | 修 bug 但必须保持函数签名 | bugfix, constraint-following |

### 任务设计四条原则

1. **验收必须是确定的** —— 每条一个 `check_cmd`，退出码 0 才算通过。
2. **前置必须失败** —— 跑 agent 之前先验一次，确保任务不是「本来就过」。
   不验的话通过率会虚高，而且**验收脚本写错了也发现不了**。
   这条有专门的测试守着（`test_every_task_actually_starts_broken`）。
3. **纯标准库、`python3` 可跑** —— 沙箱镜像和宿主都有 `python3`，但没有 `pytest`。
   用 pytest 会让任务因为**环境**失败，而不是因为**模型不行**，那样测出来的东西没意义。
4. **故意小而集中** —— 每条只考一种能力，失败时能定位到具体哪一环弱。

---

## 指标

| 指标 | 含义 |
|---|---|
| `passed` | 验收是否通过 |
| `steps` | 走了多少步（LLM 往返次数） |
| `tool_calls` | 调用了几次工具 |
| `prompt_tokens` / `completion_tokens` | 消耗 |
| `duration_s` | 墙钟耗时 |
| `compactions` | 本次运行触发了几次上下文压缩 |
| `diff_stat` | 最终改动规模（`git diff --stat` 最后一行） |

---

## 隔离与安全

- **独立数据库**：评估用临时 SQLite（`AGENT_DB_PATH`），绝不碰真实会话历史。
  ⚠️ `paths.DB_PATH` 是**导入时**读的，所以 `harness.isolate_env()` 必须在
  import 任何 `src` 模块之前调用 —— `eval/run.py` 就是这么写的。
- **空 MCP 配置**：评估要可复现，不能被本机装的插件影响。
- **工作目录在 HOME 下**：路径白名单只放行用户主目录，放 `/tmp` 会被直接拒。
- **`auto_confirm=True`**：评估里没人点确认，等价于本次运行强制等级 3。
  这是评估专用的放权，正常使用路径不受影响。每个任务都在临时目录里跑，跑完即删。

---

## 两条使用建议

**① 一次只改一个东西。** 同时改提示词和压缩阈值，跑出来变好了也不知道是哪个起的作用。

**② 看指标要看全套。** 只看通过率会被骗：
- 通过率不变但**步数涨了 30%** → 变差了（更慢更贵）
- 通过率升了但**token 翻倍** → 可能不值
- `compactions > 0` → 说明长任务真的触发了压缩，这个任务可以用来验证压缩改动

---

## 基线（首次实测）

模型 `deepseek-chat`，10 条任务：

| 任务 | 结果 | 步数 | 工具调用 | tokens | 耗时 |
|---|---|---|---|---|---|
| `fix_failing_test` | ✅ | 6 | 8 | 22,409 | 7.5s |
| `fix_off_by_one` | ✅ | 6 | 7 | 22,671 | 8.8s |
| `fix_import_error` | ✅ | 6 | 6 | 21,610 | 6.5s |
| `implement_function` | ✅ | 5 | 6 | 18,328 | 5.1s |
| `add_validation` | ✅ | 5 | 6 | 18,559 | 5.7s |
| `multi_file_rename` | ✅ | 5 | 9 | 19,368 | 5.6s |
| `extract_function` | ✅ | 6 | 7 | 22,454 | 7.4s |
| `fix_three_bugs` | ✅ | 6 | 9 | 23,659 | 7.2s |
| `keep_public_api` | ✅ | 8 | 7 | 29,239 | 11.3s |
| `find_bug_in_large_file` | ✅ | 5 | 6 | 18,874 | 5.1s |

**汇总：通过 10/10（100%）· 平均 5.8 步 · "
f"217,171 tokens · 70.2s · 压缩 0 次**

### ⚠️ 满分意味着「没有上升空间」

10/10 说明当前模型在这个难度上**全部能做对**。所以：

- **能检测退步**：改坏了 → 通过率掉下来，立刻看得见 ✅
- **检测不出进步**：没有失败的题可以"修好" ❌
- **但次级指标仍有区分度**：通过率不变而**平均步数从 5.8 涨到 9**，就是明确的退步

所以现阶段把它当**回归网**用，不要指望它告诉你"变聪明了"。
要提升区分度就得加更难的题（比如需要 20+ 步、跨多个文件保持一致性的任务）。

### ⚠️ 目前没有覆盖上下文压缩

10 条任务的上下文都远低于 32K 预算，所以 `compactions` 全是 0 ——
**这个评估集还测不到压缩路径**。要覆盖它需要一条"必须读很多文件"的长任务
（成本会明显高于其它任务，所以没放进默认集）。

---

## 已知边界

- **样本少**：8 条任务的通过率波动很大，**一次通过率变化 1 条不足以说明问题**。
  要更可信的结论就多跑几轮取平均，或者扩任务集。
- **只测单轮任务**：没有测多轮对话、中断续跑、多 Agent 模式。
- **不进 CI**：要真金白银和时间，靠手动跑。但**装置本身**有 15 个测试
  （`src/tests/test_eval_harness.py`，用假 LLM），保证它不会静默给出错误结论。
