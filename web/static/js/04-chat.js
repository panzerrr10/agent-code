function escapeHtml(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function addUserMessage(text, time, images, attachments) {
    const div = document.createElement("div");
    div.className = "msg user";
    const timeStr = time || nowTime();
    const hasImgs = images && images.length;
    const atts = attachments || [];
    const attHtml = atts.length
        ? `<div class="bubble-attachments">` + atts.map(a => `
                    <span class="attach-chip" title="${escapeHtml(a.path || "")}">
                        <span class="attach-name">📄 ${escapeHtml(a.name || "附件")}</span>
                        <button type="button" onclick="openFile('${escapeHtml(a.path || "").replace(/'/g, "\\'")}')">打开</button>
                        <button type="button" onclick="downloadPath('${escapeHtml(a.path || "").replace(/'/g, "\\'")}', '${escapeHtml(a.name || "").replace(/'/g, "\\'")}')">⬇</button>
                    </span>`).join("") + `</div>`
        : "";
    const bubbleHtml = (hasImgs
        ? (`<div class="bubble-images">` +
            images.map(src => `<img class="bubble-img" src="${src}" alt="图片" onclick="openLightbox('${src}')">`).join("") +
                    `</div>` + (text ? escapeHtml(text) : ""))
        : escapeHtml(text)) + attHtml;
    div.innerHTML = `
                <div class="bubble-wrap">
                    <div class="bubble">${bubbleHtml}</div>
                    <div class="meta-row">
                        <span class="time">${timeStr}</span>
                        <button class="msg-action-btn" title="编辑" onclick="editMessage(this)">✏️</button>
                        <button class="msg-action-btn" title="重新生成" onclick="regenerate(this)">🔄</button>
                    </div>
                </div>
    `;
    // 存原始文本
    div.dataset.rawText = text;
    chat.appendChild(div);
    chat.scrollTop = chat.scrollHeight;
}

function addAssistantMessage(time) {
    const div = document.createElement("div");
    const timeStr = time || nowTime();
    div.className = "msg assistant";
    div.innerHTML = `
                <div class="bubble-wrap">
                    <div class="bubble"></div>
                    <div class="meta-row">
                        <span class="time">${timeStr}</span>
                        <button class="msg-action-btn" title="复制" onclick="copyMessage(this)">📋</button>
                        <button class="msg-action-btn" title="重新生成" onclick="regenerate(this)">🔄</button>
                    </div>
                    <details class="exec-details" style="display: none;">
                        <summary>
                            <span class="arrow"></span>
                            <span class="exec-title">执行详情</span>
                            <span class="token-info"></span>
                        </summary>
                        <div class="exec-list"></div>
                    </details>
                </div>
    `;
    chat.appendChild(div);
    chat.scrollTop = chat.scrollHeight;
    return {
        div: div,
        bubble: div.querySelector(".bubble"),
        details: div.querySelector(".exec-details"),
        execList: div.querySelector(".exec-list"),
        execTitle: div.querySelector(".exec-title"),
        tokenInfo: div.querySelector(".token-info"),
    };
}

/** 会改磁盘的工具 —— 跑完后把它们产出/改过的文件列出来供下载。 */
const FILE_WRITING_TOOLS = new Set(["write_file", "create_file", "edit_file", "multi_edit"]);

function collectProduced(ctx, toolName, toolArgs) {
    if (!FILE_WRITING_TOOLS.has(toolName) || !toolArgs) return;
    ctx.produced = ctx.produced || new Map();
    if (toolArgs.path) {
        const p = String(toolArgs.path);
        ctx.produced.set(p, p.split("/").pop());
    }
    if (toolName === "multi_edit" && Array.isArray(toolArgs.edits)) {
        toolArgs.edits.forEach(e => {
            if (e && e.path) {
                const p = String(e.path);
                ctx.produced.set(p, p.split("/").pop());
            }
        });
    }
}

// ===== 「跳到最新」=====
// 现在流式期间无条件 `chat.scrollTop = chat.scrollHeight`，
// 用户往上翻看历史时会被**一直拽回底部** —— 根本没法读。
// 改成：用户本来就在底部才跟随；往上翻了就显示一个浮动按钮。
let chatSticky = true;        // 是否「粘」在底部
let pendingNewCount = 0;      // 用户翻上去之后又来了几条

function chatNearBottom(slack = 80) {
    return chat.scrollHeight - chat.scrollTop - chat.clientHeight <= slack;
}

function scrollChatToBottom(smooth) {
    chatSticky = true;
    pendingNewCount = 0;
    updateJumpLatest();
    chat.scrollTo({ top: chat.scrollHeight, behavior: smooth ? "smooth" : "auto" });
}

function jumpToLatest() { scrollChatToBottom(true); }

function updateJumpLatest() {
    const btn = document.getElementById("jumpLatest");
    if (!btn) return;
    const show = !chatSticky && chat.scrollHeight > chat.clientHeight + 40;
    btn.style.display = show ? "flex" : "none";
    const txt = btn.querySelector(".jl-text");
    if (txt) txt.textContent = pendingNewCount ? `${pendingNewCount} 条新内容` : "";
}

/** 流式期间要滚动时统一走这里：只在用户本来就在底部时才真滚。 */
function followChat() {
    if (chatSticky) {
        chat.scrollTop = chat.scrollHeight;
    } else {
        pendingNewCount += 1;
        updateJumpLatest();
    }
}

// 用户手动滚动时更新 sticky 状态
chat.addEventListener("scroll", () => {
    const atBottom = chatNearBottom();
    if (atBottom && !chatSticky) { chatSticky = true; pendingNewCount = 0; }
    else if (!atBottom && chatSticky) { chatSticky = false; }
    updateJumpLatest();
}, { passive: true });

// ===== 执行详情：分组折叠与筛选 =====
// ===== 执行详情：分组折叠与筛选 =====
// 长任务里几十条工具调用平铺成一坨，展开后要滚很久，
// 而且**失败的那条淹在中间**。所以跑完做一次整理：
//   ① 失败的操作**置顶**并展开
//   ② 只保留最近 N 条在外，更早的收进可展开的「更早的 N 个操作」
//   ③ 一个筛选行：全部 / 写文件 / 失败

const EXEC_KEEP_RECENT = 4;
const EXEC_WRITE_TOOLS = new Set(["write_file", "create_file", "edit_file",
                                  "multi_edit", "delete_file"]);

function finalizeExecList(ctx) {
    const list = ctx && ctx.execList;
    if (!list) return;
    const items = [...list.querySelectorAll(":scope > .exec-item")];
    if (!items.length) return;

    // 已经整理过就别重复（续跑会再走一次）
    list.querySelector(".exec-filter")?.remove();
    const oldWrap = list.querySelector(".exec-older");
    if (oldWrap) { oldWrap.replaceWith(...oldWrap.querySelectorAll(".exec-item")); }

    const fresh = [...list.querySelectorAll(":scope > .exec-item")];
    const fails = fresh.filter(el => el.classList.contains("is-fail"));
    const writes = fresh.filter(el => EXEC_WRITE_TOOLS.has(el.dataset.tool));

    // 筛选行
    const bar = document.createElement("div");
    bar.className = "exec-filter";
    bar.innerHTML = `
                <button type="button" class="exec-fbtn active" data-f="all">全部 <b>${fresh.length}</b></button>
                <button type="button" class="exec-fbtn" data-f="write">写文件 <b>${writes.length}</b></button>
                <button type="button" class="exec-fbtn" data-f="fail">失败 <b>${fails.length}</b></button>`;
    bar.addEventListener("click", (ev) => {
        const b = ev.target.closest(".exec-fbtn");
        if (!b) return;
        bar.querySelectorAll(".exec-fbtn").forEach(x => x.classList.toggle("active", x === b));
        applyExecFilter(list, b.dataset.f);
    });
    list.prepend(bar);

    // 更早的收起来。
    // **必须先确定「保留集合」再置顶。** 反过来做过一次并且踩了坑：
    // 先把失败移到最前，再 `slice(0, n-keep)` 切「更早的」，
    // 等于刚置顶的失败立刻又被折进折叠块里 —— 白置顶。
    // 正确做法：保留集合 = 最近 N 条 ∪ 全部失败，其余才算「更早的」。
    const all = [...list.querySelectorAll(":scope > .exec-item")];
    const keepSet = new Set(all.slice(-EXEC_KEEP_RECENT));
    fails.forEach(el => keepSet.add(el));
    const older = all.filter(el => !keepSet.has(el));

    // 失败置顶：按原顺序插到筛选行后面
    for (const el of fails) list.insertBefore(el, bar.nextSibling);

    if (older.length > 1) {
        const wrap = document.createElement("details");
        wrap.className = "exec-older";
        const nFail = older.filter(el => el.classList.contains("is-fail")).length;
        wrap.innerHTML = `<summary>▸ 更早的 ${older.length} 个操作`
            + (nFail ? `（含 ${nFail} 个失败）` : "") + `</summary>`;
        const box = document.createElement("div");
        box.className = "exec-older-box";
        older.forEach(el => box.appendChild(el));
        wrap.appendChild(box);
        list.appendChild(wrap);
    }
    applyExecFilter(list, "all");
}

function applyExecFilter(list, mode) {
    const items = [...list.querySelectorAll(".exec-item")];
    items.forEach(el => {
        let show = true;
        if (mode === "write") show = EXEC_WRITE_TOOLS.has(el.dataset.tool);
        else if (mode === "fail") show = el.classList.contains("is-fail");
        el.style.display = show ? "" : "none";
    });
    // 筛选时把「更早的」也自动展开，否则筛了却看不见
    const wrap = list.querySelector(".exec-older");
    if (wrap && mode !== "all") wrap.open = true;
}

/**
 * 收尾：把流式期间的临时芯片换成「第 N 轮改动」卡片。
 *
 * 临时芯片只知道「碰过哪些文件」，卡片能给出**每处改动多少行**，
 * 并且每一行都能点开看**本轮 diff**。
 */
function finishRoundCard(ctx, sid) {
    if (!ctx || !ctx.bubble || !ctx.bubble.parentElement) return;
    const live = ctx.bubble.parentElement.querySelector(".bubble-attachments");
    if (live) live.remove();
    if (ctx.runIndex && sid) {
        renderRoundCard(ctx.bubble.parentElement, sid, ctx.runIndex, null);
    }
}

/**
 * 上下文压缩状态。
 *
 * **必须让用户看得见** —— 压缩最大的风险是「悄悄剪掉了关键信息」，
 * 有这个指示器就能一眼确认它在工作、以及压到了什么程度。
 */
function updateContextStats(ctx, ev) {
    if (!ctx || !ctx.bubble || !ctx.bubble.parentElement) return;
    let el = ctx.bubble.parentElement.querySelector(".ctx-stats");
    if (!el) {
        el = document.createElement("div");
        el.className = "ctx-stats";
        ctx.bubble.parentElement.appendChild(el);
    }
    const pct = Math.round((ev.after_tokens / ev.budget) * 100);
    const parts = [`📉 上下文 ${(ev.after_tokens / 1000).toFixed(1)}K / ${(ev.budget / 1000).toFixed(0)}K (${pct}%)`];
    if (ev.pruned) parts.push("已剪枝旧工具结果");
    if (ev.folded) parts.push(`已折叠 ${ev.folded} 个步骤`);
    parts.push(`省下 ${(ev.saved_chars / 1000).toFixed(0)}K 字符`);
    el.textContent = parts.join(" · ");
    el.title = `压缩前 ${ev.before_tokens} tokens → 压缩后 ${ev.after_tokens} tokens`;
}

/** 本次运行加载了项目约定时给个小提示（不做成卡片，避免干扰）。 */
function appendProjectContextNote(ctx, ev) {
    if (!ctx || !ctx.bubble || !ctx.bubble.parentElement) return;
    const names = (ev.sources || []).map(x => x.path.split("/").pop()).join("、");
    const div = document.createElement("div");
    div.className = "pc-note";
    div.textContent = `📋 已加载项目约定：${names || "AGENTS.md"}（${ev.chars} 字符${ev.truncated ? "，已截断" : ""}）`;
    ctx.bubble.parentElement.appendChild(div);
}

/** 等级 1 下工具被拒时的提示卡片。 */
function appendPermissionDenied(ctx, content) {
    if (!ctx || !ctx.bubble || !ctx.bubble.parentElement) return;
    const div = document.createElement("div");
    div.className = "bubble permission-denied";
    div.textContent = content || "⛔ 当前会话为只读模式，已拒绝该操作";
    ctx.bubble.parentElement.appendChild(div);
}

/** 一条工具调用的 HTML（实时与历史共用，保证两处外观一致）。 */
function toolItemHtml(toolName, toolArgs, icon, pretty) {
    if (pretty === undefined) {
        try { pretty = JSON.stringify(toolArgs, null, 2); } catch (e) { pretty = String(toolArgs); }
    }
    return `
                <details class="tool-details">
                    <summary>
                        <span class="tool-name">${icon || "🔧"} ${escapeHtml(toolName)}</span>
                        <span class="tool-brief">${escapeHtml(toolSummary(toolName, toolArgs))}</span>
                    </summary>
                    <div class="tool-full">
                        <div class="tool-full-label">完整参数 / 指令</div>
                        <pre class="tool-args">${escapeHtml(pretty)}</pre>
                        <div class="tool-full-label">执行结果</div>
                        <div class="tool-result">⏳ 执行中...</div>
                    </div>
                </details>`;
}

// ===== 「第 N 轮改动」卡片（需求 4/5/7）=====
const ROUND_BADGE = {
    create: { cls: "create", text: "A" },
    delete: { cls: "delete", text: "D" },
    modify: { cls: "modify", text: "M" },
};

/**
 * 渲染某一轮的改动卡片。
 *
 * - 默认收起，点标题展开
 * - 展开后列表**固定高度可滚动**（改了几十个文件也不会把对话撑爆）
 * - 点某一行 → openDiff() → 右侧只读 diff（与点项目树完全隔离）
 */
function renderRoundCard(anchorEl, sessionId, runIndex, pre) {
    if (!anchorEl) return;
    const render = (j) => {
        if (!j || !j.ok || !(j.files || []).length) return;
        const files = j.files;
        const box = document.createElement("div");
        box.className = "round-card";
        const items = files.map(f => {
            const b = ROUND_BADGE[f.change_type] || ROUND_BADGE.modify;
            const nm = String(f.path).split("/").pop();
            return `
                    <div class="round-item" title="${escapeHtml(f.path)}">
                        <span class="round-badge ${b.cls}">${b.text}</span>
                        <span class="round-file">${escapeHtml(nm)}</span>
                        <span class="round-stat">+${f.added} −${f.removed}</span>
                        <span class="round-actions">
                            <button type="button" class="round-btn"
                                onclick="openDiff(${sessionId}, ${runIndex}, '${escapeHtml(f.path).replace(/'/g, "\\'")}')"
                                title="在右侧查看本轮改动（只读）">查看改动</button>
                            <button type="button" class="round-btn"
                                onclick="openFile('${escapeHtml(f.path).replace(/'/g, "\\'")}')"
                                title="在右侧打开文件内容（可编辑）">打开</button>
                        </span>
                    </div>`;
        }).join("");
        box.innerHTML = `
                    <div class="round-head" onclick="toggleRoundCard(this)"
                         tabindex="0" role="button" aria-label="展开或收起本轮改动"
                         onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();toggleRoundCard(this);}">
                        <span class="round-arrow">▸</span>
                        <span class="round-title">📝 第 ${runIndex} 轮改动</span>
                        <span class="round-meta">${files.length} 个文件 · +${j.added} −${j.removed}</span>
                    </div>
                    <div class="round-list" style="display: none;">${items}</div>`;
        anchorEl.appendChild(box);
    };
    if (pre) { render(pre); return; }
    fetch(`/api/run_changes?session_id=${sessionId}&run_index=${runIndex}`)
        .then(r => r.json())
        .then(render)
        .catch(e => console.warn("[round] 读取改动失败", e));
}

function toggleRoundCard(headEl) {
    const list = headEl.nextElementSibling;
    const arrow = headEl.querySelector(".round-arrow");
    const open = list.style.display !== "none";
    list.style.display = open ? "none" : "block";
    if (arrow) arrow.textContent = open ? "▸" : "▾";
}

/** 历史轮次卡片：第 N 轮挂到第 N 条 assistant 消息下面（刷新页面也能回看）。 */
async function attachHistoryRoundCards(sessionId, roundEls) {
    try {
        const res = await fetch(`/api/run_changes/history?session_id=${sessionId}`);
        const j = await res.json();
        if (!j.ok) return;
        for (const r of (j.rounds || [])) {
            const el = roundEls[r.run_index];
            if (el) renderRoundCard(el, sessionId, r.run_index, null);
        }
    } catch (e) {
        console.warn("[round] 历史改动读取失败", e);
    }
}

/** 工具调用的一行摘要：优先显示「到底执行了什么指令」。 */
function toolSummary(toolName, args) {
    if (!args || typeof args !== "object") return "";
    if (args.cmd) return String(args.cmd);                       // run_command：完整命令
    if (args.code) return String(args.code).split("\n")[0].slice(0, 70);
    if (Array.isArray(args.edits)) return `${args.edits.length} 处编辑`;
    if (args.path) return String(args.path);
    if (args.query) return String(args.query);
    if (args.task) return String(args.task).slice(0, 70);
    try { return JSON.stringify(args).slice(0, 70); } catch (e) { return ""; }
}

const TOOL_ICONS = {
    read_file: "📖", read_file_range: "📖", list_dir: "📁", search_files: "🔍",
    write_file: "💾", create_file: "✨", edit_file: "✏️", multi_edit: "✏️",
    delete_file: "🗑️", run_command: "▶️", run_python: "🐍", run_tests: "🧪",
    git_status: "🌿", git_diff: "🌿", git_log: "🌿", git_commit: "🌿",
    create_plan: "📋", update_plan: "📋", search_knowledge: "📚",
};

/**
 * 一条工具调用（**可点开**）。
 *
 * 点「做了什么」展开，能看到**完整参数与完整输出** ——
 * 尤其是 run_command 的完整命令行、edit_file 的完整改动参数。
 */
function appendToolCall(ctx, toolName, toolArgs) {
    ctx.details.style.display = "block";
    const count = ctx.execList.children.length + 1;
    ctx.execTitle.textContent = `执行详情（${count} 个工具调用）`;
    const item = document.createElement("div");
    item.className = "exec-item";
    item.dataset.tool = toolName;          // 分组与筛选用
    const icon = TOOL_ICONS[toolName] || "🔧";
    let pretty = "";
    try { pretty = JSON.stringify(toolArgs, null, 2); } catch (e) { pretty = String(toolArgs); }
    item.innerHTML = toolItemHtml(toolName, toolArgs, icon, pretty);
    ctx.execList.appendChild(item);
    followChat();      // 用户翻上去看历史时不要把他拽下来
    return item.querySelector(".tool-result");
}

function updateToolResult(resultEl, content) {
    resultEl.textContent = "📋 " + content;
    // 失败的操作用红色标出来。长任务里几十条操作平铺，
    // 失败那条淹在中间基本找不到 —— 所以不只上色，还要在收尾时置顶。
    const item = resultEl.closest(".exec-item");
    if (item) {
        const failed = String(content || "").trimStart().startsWith("❌");
        item.classList.toggle("is-fail", failed);
        const badge = item.querySelector(".tool-details > summary");
        if (badge && failed && !badge.querySelector(".fail-tag")) {
            const t = document.createElement("span");
            t.className = "fail-tag";
            t.textContent = "失败";
            badge.appendChild(t);
        }
    }
}

// 从数据库恢复一条 AI 消息
function restoreAssistantMessage(raw, time) {
    // 尝试解析 JSON（含 answer + tools）
    let answer = raw;
    let tools = [];
    try {
        const obj = JSON.parse(raw);
        if (obj.answer !== undefined) {
            answer = obj.answer;
            tools = obj.tools || [];
            tokens = obj.tokens || null;
        }
    } catch (e) { }

    const ctx = addAssistantMessage(time);
    ctx.restoredRoundTools = tools;
    ctx.bubble.innerHTML = DOMPurify.sanitize(marked.parse(answer || ""));
    // 高亮代码块
    ctx.bubble.querySelectorAll('pre code').forEach(block => {
        if (!block.dataset.highlighted) {
            hljs.highlightElement(block);
            block.dataset.highlighted = "yes";
        }
    });

    if (tools.length > 0) {
        ctx.details.style.display = "block";
        let count = 0;
        let lastResultEl = null;
        tools.forEach(t => {
            if (t.type === "tool_call") {
                count++;
                ctx.execTitle.textContent = `执行详情（${count} 个工具调用）`;
                const item = document.createElement("div");
                item.className = "exec-item";
                item.innerHTML = toolItemHtml(
                    t.name, t.args, TOOL_ICONS[t.name] || "🔧");
                ctx.execList.appendChild(item);
                lastResultEl = item.querySelector(".tool-result");
            } else if (t.type === "tool_result" && lastResultEl) {
                lastResultEl.textContent = "📋 " + t.content;
            }
        });
    }
    // ✅ 恢复 token 用量
    if (tokens) {
        ctx.details.style.display = "block";
        ctx.tokenInfo.innerHTML =
                    `⚡ <span class="token-num">${tokens.total_tokens}</span> tokens ` +
                    `(输入 ${tokens.prompt_tokens} · 输出 ${tokens.completion_tokens} · ${tokens.steps} 步)`;
    }
    return ctx;   // 历史轮次要往这条消息下面挂「第 N 轮改动」卡片
}
// ============ 会话管理 ============
