// ===== 工作区 =====
// 工作区 = 一组会话 + 一个工作目录。会话挂在某个工作区下，
// 该工作区的位置就是这些会话的工作目录（不再用前端 localStorage 存路径）。
let workspaces = [];
let currentWorkspaceId = null;
let wsDefaultPath = "/home/pz/PycharmProjects";
let wsEditingId = null;   // 弹窗处于编辑态时的工作区 id；null 表示新建

async function loadWorkspaces() {
    try {
        const res = await fetch("/workspaces");
        const j = await res.json();
        workspaces = j.workspaces || [];
        wsDefaultPath = j.default_path || wsDefaultPath;
        const dp = document.getElementById("wsDefaultPath");
        if (dp) dp.textContent = wsDefaultPath;

        const saved = Number(localStorage.getItem("currentWorkspaceId")) || null;
        currentWorkspaceId = workspaces.some(w => w.id === saved)
            ? saved
            : (workspaces[0] ? workspaces[0].id : null);

        renderWorkspaceSelector();
        renderWorkspaceMenu();
    } catch (e) {
        console.error("加载工作区失败", e);
    }
}

function currentWorkspace() {
    return workspaces.find(w => w.id === currentWorkspaceId) || null;
}

function renderWorkspaceSelector() {
    const ws = currentWorkspace();
    const nameEl = document.getElementById("wsCurrentName");
    const pathEl = document.getElementById("wsCurrentPath");
    const newBtn = document.getElementById("newChatBtn");
    if (!ws) {
        nameEl.textContent = "（无工作区）";
        pathEl.textContent = "";
        if (newBtn) newBtn.textContent = "+ 新对话";
        return;
    }
    nameEl.textContent = ws.name;
    // 侧边栏很窄，完整路径一定会被截断成「/home/pz/PycharmProje...」。
    // 显示末级目录名更认得出来，完整路径放 title 悬停可见。
    const segs = String(ws.path || "").split("/").filter(Boolean);
    const short = segs.length ? segs[segs.length - 1] : ws.path;
    pathEl.textContent = ws.exists ? short : short + "  ⚠️ 不存在";
    pathEl.title = ws.exists ? ws.path : ws.path + "（不存在）";
    if (newBtn) newBtn.textContent = `+ 在「${ws.name}」新建会话`;
}

function renderWorkspaceMenu() {
    const menu = document.getElementById("wsMenu");
    const rows = workspaces.map(w => `
                <div class="ws-item ${w.id === currentWorkspaceId ? "active" : ""}" data-ws="${w.id}">
                    <span class="ws-badge">${w.id === currentWorkspaceId ? "📂" : "📁"}</span>
                    <div class="ws-item-main">
                        <div class="ws-item-name">${escapeHtml(w.name)}</div>
                        <div class="ws-item-path ${w.exists ? "" : "ws-bad"}"
                             title="${escapeHtml(w.path)}">${escapeHtml(w.path)}${w.exists ? "" : "（不存在）"}</div>
                    </div>
                    <span class="ws-item-count">${w.session_count}</span>
                    <div class="ws-item-actions">
                        <button data-act="edit" data-id="${w.id}" title="编辑">✎</button>
                        ${workspaces.length > 1
                            ? `<button data-act="del" data-id="${w.id}" title="删除">🗑</button>` : ""}
                    </div>
                </div>`).join("");
    menu.innerHTML = rows + `
                <div class="ws-menu-sep"></div>
                <div class="ws-menu-new" data-act="new">＋ 新建工作区…</div>`;
}

function toggleWsMenu(ev) {
    if (ev) ev.stopPropagation();
    const menu = document.getElementById("wsMenu");
    const open = menu.style.display !== "none";
    if (open) {
        menu.style.display = "none";
    } else {
        renderWorkspaceMenu();
        menu.style.display = "block";
    }
}

function closeWsMenu() {
    const menu = document.getElementById("wsMenu");
    if (menu) menu.style.display = "none";
}

async function selectWorkspace(id) {
    if (id === currentWorkspaceId) { closeWsMenu(); return; }
    snapshotTabs();                       // 用「旧的」currentWorkspaceId 快照（必须在这里做）
    currentWorkspaceId = id;
    localStorage.setItem("currentWorkspaceId", String(id));
    closeWsMenu();
    renderWorkspaceSelector();
    // 右侧文件面板也跟着换到本工作区的那一套
    await switchWorkspaceTabs(id);
    // 会话是按工作区过滤的：换工作区就要重新载入列表（没有会话会自动建一个）
    currentSessionId = null;
    await loadSessions();
    await loadProjectTree();
}

// ---- 新建 / 编辑工作区弹窗 ----
function openWsModal(id) {
    wsEditingId = id || null;
    const ws = id ? workspaces.find(w => w.id === id) : null;
    document.getElementById("wsModalTitle").textContent = ws ? "编辑工作区" : "新建工作区";
    document.getElementById("wsSubmitBtn").textContent = ws ? "保存" : "创建";
    document.getElementById("wsNameInput").value = ws ? ws.name : "";
    document.getElementById("wsPathInput").value = ws ? ws.path : "";
    document.getElementById("wsModalError").style.display = "none";
    document.getElementById("wsModal").style.display = "flex";
    setTimeout(() => document.getElementById("wsNameInput").focus(), 30);
}

function closeWsModal() {
    document.getElementById("wsModal").style.display = "none";
    wsEditingId = null;
}

async function pickWorkspaceFolder() {
    const btn = document.querySelector("#wsModal .fld-row button");
    if (btn) { btn.disabled = true; btn.textContent = "选择中…"; }
    try {
        const res = await fetch("/api/select_folder", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                title: "选择工作区文件夹",
                initial: document.getElementById("wsPathInput").value.trim() || wsDefaultPath,
            }),
        });
        const data = await res.json();
        if (data.ok && data.path) {
            document.getElementById("wsPathInput").value = data.path;
            // 名称留空时，顺手用目录名填上，省得再手打
            const nameEl = document.getElementById("wsNameInput");
            if (!nameEl.value.trim()) {
                const parts = data.path.replace(/\/+$/, "").split("/");
                nameEl.value = parts[parts.length - 1] || "";
            }
        } else if (!data.cancelled) {
            // 用户主动取消不该报错
            showWsError(data.error || "打开文件夹选择器失败");
        }
    } catch (e) {
        showWsError("弹窗失败：" + e.message);
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = "📂 选择文件夹"; }
    }
}

function showWsError(msg) {
    const el = document.getElementById("wsModalError");
    el.textContent = msg;
    el.style.display = "block";
}

async function submitWorkspace() {
    const name = document.getElementById("wsNameInput").value.trim();
    const path = document.getElementById("wsPathInput").value.trim();
    const btn = document.getElementById("wsSubmitBtn");
    btn.disabled = true;
    try {
        const url = wsEditingId ? `/workspaces/${wsEditingId}` : "/workspaces";
        const res = await fetch(url, {
            method: wsEditingId ? "PATCH" : "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name, path }),
        });
        const j = await res.json();
        if (!j.ok) { showWsError(j.error || "保存失败"); return; }
        const newId = j.id || wsEditingId;
        closeWsModal();
        await loadWorkspaces();
        if (!wsEditingId && newId) {
            // 新建后直接切过去，符合「建完就想在这儿干活」的直觉
            currentWorkspaceId = newId;
            localStorage.setItem("currentWorkspaceId", String(newId));
            renderWorkspaceSelector();
            currentSessionId = null;
            await loadSessions();
        }
    } catch (e) {
        showWsError("保存失败：" + e.message);
    } finally {
        btn.disabled = false;
    }
}

async function deleteWorkspace(id, ev) {
    if (ev) ev.stopPropagation();
    const ws = workspaces.find(w => w.id === id);
    if (!ws) return;
    const extra = ws.session_count
        ? `\n注意：该工作区下的 ${ws.session_count} 个会话及其聊天记录会一并删除。`
        : "";
    if (!confirm(`删除工作区「${ws.name}」？${extra}\n（只会删记录，不会动磁盘上的目录）`)) return;
    const res = await fetch(`/workspaces/${id}`, { method: "DELETE" });
    const j = await res.json();
    if (!j.ok) { alert(j.error || "删除失败"); return; }
    if (id === currentWorkspaceId) {
        currentSessionId = null;
        localStorage.removeItem("currentWorkspaceId");
    }
    await loadWorkspaces();
    await loadSessions();
}

(function bindWsMenu() {
    const menu = document.getElementById("wsMenu");
    if (!menu) return;
    menu.addEventListener("click", (ev) => {
        // 注意顺序：先判断「行内的小按钮」，再判断「整行」。
        // 之前只找 [data-act]，而工作区行身上只有 data-ws、没有 data-act，
        // 于是点整行直接 return —— 表现为「点了没反应，切不过去」。
        const btn = ev.target.closest("button[data-act]");
        if (btn) {
            ev.stopPropagation();
            const a = btn.dataset.act;
            const id = Number(btn.dataset.id);
            if (a === "edit") { closeWsMenu(); openWsModal(id); return; }
            if (a === "del") { deleteWorkspace(id, ev); return; }
        }
        const newEntry = ev.target.closest("[data-act='new']");
        if (newEntry) {
            ev.stopPropagation();
            closeWsMenu();
            openWsModal();
            return;
        }
        // 点行内任意位置都算「选中这个工作区」
        const row = ev.target.closest(".ws-item[data-ws]");
        if (row) {
            ev.stopPropagation();
            selectWorkspace(Number(row.dataset.ws));
        }
    });
    // 点空白处收起菜单
    document.addEventListener("click", (ev) => {
        if (!ev.target.closest(".ws-selector")) closeWsMenu();
    });
    document.addEventListener("keydown", (ev) => {
        if (ev.key !== "Escape") return;
        closeWsMenu();
        closeWsModal();
    });
})();

async function loadSessions() {
    try {
        const q = currentWorkspaceId ? `?workspace_id=${currentWorkspaceId}` : "";
        const res = await fetch("/sessions" + q);
        const sessions = await res.json();
        renderSessionList(sessions);

        // 当前会话不属于这个工作区时，切到本工作区的第一个
        const belongs = sessions.some(s => s.id === currentSessionId);
        if (!belongs) {
            if (sessions.length > 0) {
                await switchSession(sessions[0].id);
            } else {
                await createNewSession();
            }
        } else {
            highlightSession(currentSessionId);
        }
    } catch (e) {
        console.error("加载会话失败", e);
    }
}
// ===== 对话搜索 =====
let searchMatches = [];
let searchIndex = -1;

function openSearch() {
    document.getElementById("searchBox").style.display = "flex";
    document.getElementById("searchInput").focus();
}

function closeSearch() {
    document.getElementById("searchBox").style.display = "none";
    document.getElementById("searchInput").value = "";
    clearSearchHighlights();
    searchMatches = [];
    searchIndex = -1;
    document.getElementById("searchCount").textContent = "";
}

function clearSearchHighlights() {
    document.querySelectorAll(".msg.search-match, .msg.search-active").forEach(el => {
        el.classList.remove("search-match", "search-active");
    });
}

function performSearch(keyword) {
    clearSearchHighlights();
    searchMatches = [];
    searchIndex = -1;

    if (!keyword || !keyword.trim()) {
        document.getElementById("searchCount").textContent = "";
        return;
    }

    const kw = keyword.toLowerCase();
    document.querySelectorAll(".msg").forEach(msg => {
        const text = msg.innerText.toLowerCase();
        if (text.includes(kw)) {
            msg.classList.add("search-match");
            searchMatches.push(msg);
        }
    });

    if (searchMatches.length > 0) {
        searchIndex = 0;
        searchMatches[0].classList.add("search-active");
        searchMatches[0].scrollIntoView({ behavior: "smooth", block: "center" });
    }

    document.getElementById("searchCount").textContent =
                `${searchMatches.length} 条匹配`;
}

function nextMatch() {
    if (searchMatches.length === 0) return;
    searchMatches[searchIndex].classList.remove("search-active");
    searchIndex = (searchIndex + 1) % searchMatches.length;
    searchMatches[searchIndex].classList.add("search-active");
    searchMatches[searchIndex].scrollIntoView({ behavior: "smooth", block: "center" });
}

// 绑定事件
document.addEventListener("DOMContentLoaded", () => {
    const input = document.getElementById("searchInput");
    if (input) {
        input.addEventListener("input", (e) => performSearch(e.target.value));
        input.addEventListener("keydown", (e) => {
            if (e.key === "Enter") {
                e.preventDefault();
                nextMatch();
            } else if (e.key === "Escape") {
                closeSearch();
            }
        });
    }
    // 注意：这里原来还有一句 loadProjectRoot()，那个函数在改成「工作区」模型时
    // 已经删掉了。它抛的 ReferenceError 会**中断本 handler 后面的语句**，
    // 导致 loadProjectTree() / restoreProjectTreeState() 从来没执行过
    // （项目树的折叠状态因此一直恢复不了）。已删除。
    loadProjectTree();
    restoreProjectTreeState();
});

// 全局快捷键 Ctrl+F
document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "f") {
        e.preventDefault();
        openSearch();
    }
    if (e.key === "Escape") {
        const box = document.getElementById("searchBox");
        if (box && box.style.display !== "none") {
            closeSearch();
        }
    }
});

/** SQLite 的 "YYYY-MM-DD HH:MM:SS" 是 **UTC**，转成本地时间再显示。 */
function parseServerTime(str) {
    if (!str) return null;
    const d = new Date(String(str).replace(" ", "T") + "Z");
    return isNaN(d.getTime()) ? null : d;
}

/** 人类友好的相对时间：刚刚 / 12 分钟前 / 3 小时前 / 昨天 / 10-08。 */
function relTime(str) {
    const d = parseServerTime(str);
    if (!d) return "";
    const mins = Math.floor((Date.now() - d.getTime()) / 60000);
    if (mins < 1) return "刚刚";
    if (mins < 60) return `${mins} 分钟前`;
    const hours = Math.floor(mins / 60);
    if (hours < 24 && d.toDateString() === new Date().toDateString()) {
        return `${hours} 小时前`;
    }
    const y = new Date(Date.now() - 86400000);
    if (d.toDateString() === y.toDateString()) return "昨天";
    return `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

let sessionFilter = "";      // 会话搜索关键字
let allSessions = [];        // 未过滤的原始列表

function onSessionSearch(v) {
    sessionFilter = (v || "").trim().toLowerCase();
    renderSessionList(allSessions);
}

function renderSessionList(sessions) {
    allSessions = sessions || [];
    const list = document.getElementById("sessionList");
    if (!list) return;

    const shown = sessionFilter
        ? allSessions.filter(s => String(s.title || "").toLowerCase().includes(sessionFilter))
        : allSessions;

    // 搜索框只在会话够多时才占地方
    const searchBox = document.getElementById("sessionSearch");
    if (searchBox) {
        searchBox.parentElement.style.display = allSessions.length >= 8 ? "block" : "none";
    }

    list.innerHTML = "";
    if (!shown.length) {
        const empty = document.createElement("div");
        empty.className = "session-empty";
        empty.textContent = sessionFilter
            ? `没有匹配「${sessionFilter}」的会话`
            : "还没有会话";
        list.appendChild(empty);
        return;
    }

    shown.forEach(s => {
        const div = document.createElement("div");
        div.className = "session-item";
        div.dataset.id = s.id;
        div.tabIndex = 0;                       // 键盘可达
        div.setAttribute("role", "button");
        const n = Number(s.message_count || 0);
        div.innerHTML = `
                    <span class="session-title" title="${escapeHtml(s.title)}">${escapeHtml(s.title)}</span>
                    <span class="session-meta">
                        <span class="session-time">${relTime(s.updated_at)}</span>
                        ${n ? `<span class="session-count">${n} 条</span>` : ""}
                    </span>
                    <button class="session-delete" title="删除这个会话"
                            aria-label="删除会话 ${escapeHtml(s.title)}">✕</button>
        `;
        // 运行中标记
        if (sessionStates[s.id] && sessionStates[s.id].status === "running") {
            const badge = document.createElement("span");
            badge.textContent = "⏳";
            badge.style.cssText = "margin-left: 4px; font-size: 11px;";
            const titleEl = div.querySelector(".session-title");
            if (titleEl) titleEl.after(badge);
        }
        div.addEventListener("click", (e) => {
            if (e.target.classList.contains("session-delete")) return;
            switchSession(s.id);
        });
        div.addEventListener("keydown", (e) => {     // 键盘可达
            if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                switchSession(s.id);
            }
        });
        div.querySelector(".session-delete").addEventListener("click", async (e) => {
            e.stopPropagation();
            if (!confirm(`确定删除「${s.title}」吗？`)) return;
            await fetch(`/sessions/${s.id}`, { method: "DELETE" });
            if (currentSessionId === s.id) currentSessionId = null;
            forgetUsage(s.id);          // 用量/后台状态跟着会话一起清掉
            await loadSessions();
        });
        list.appendChild(div);
    });
    if (currentSessionId) highlightSession(currentSessionId);
}

function highlightSession(id) {
    document.querySelectorAll(".session-item").forEach(el => {
        el.classList.toggle("active", Number(el.dataset.id) === id);
    });
}

async function switchSession(sessionId) {
    // 上一个会话若仍在后台运行：其 SSE 继续、sessionStates 保留（不中断、不清空）
    const prevSessionId = currentSessionId;
    currentSessionId = sessionId;
    highlightSession(sessionId);
    chat.innerHTML = "";
    clearPlan();

    try {
        const res = await fetch(`/history/${sessionId}`);
        const messages = await res.json();
        // 用量用**全量**消息重建（不受下面「只渲染最近 30 条」影响）。
        // 但若本会话此刻还在跑，就**不要**覆盖 —— 历史里还没有那一轮的
        // token_usage（要等跑完才落库），覆盖会把实时累加的数字抹掉。
        const _st = sessionStates[sessionId];
        if (!_st || _st.status !== "running") {
            rebuildUsageFromHistory(sessionId, messages);
        }
        renderSessionStats(sessionId);
        // 「第 N 轮」= 第 N 条用户消息。这里记下每条 assistant 消息属于第几轮，
        // 稍后按轮次把改动卡片挂上去（刷新页面后历史轮次仍能回看）。
        let _userCount = 0;
        const _roundEls = {};

        // **长会话只渲染最近 N 条。**
        // 原来是全量渲染：几百条消息的会话（长任务很容易积累）会一次性
        // 建出上千个 DOM 节点 + 一堆 CodeMirror/marked 解析，切过去会明显卡住。
        // 更早的折成一个按钮，点了才渲染 —— 高度不固定的聊天流做虚拟滚动
        // 得不偿失，这样已经能解决绝大部分问题。
        const HISTORY_RENDER_LIMIT = 30;
        let _older = [];
        if (messages.length > HISTORY_RENDER_LIMIT) {
            _older = messages.slice(0, messages.length - HISTORY_RENDER_LIMIT);
        }
        const _renderFrom = (arr) => arr.forEach(m => {
            const t = m.created_at && m.created_at.length >= 16
                ? m.created_at.slice(0, 16) : "";
            if (m.role === "user") {
                _userCount += 1;
                // 历史里的用户消息可能是 JSON（含图片 data URL），解析后一起回显
                let utext = m.content, uimgs = [];
                try {
                    const o = JSON.parse(m.content);
                    if (o && typeof o === "object" && typeof o.text === "string") {
                        utext = o.text;
                        uimgs = Array.isArray(o.images) ? o.images : [];
                    }
                } catch (e) { /* 纯文本消息，保持原样 */ }
                addUserMessage(utext, t, uimgs);
            } else {
                const _ctx = restoreAssistantMessage(m.content, t);
                if (_userCount && _ctx && _ctx.bubble) {
                    _roundEls[_userCount] = _ctx.bubble.parentElement;
                }
            }
        });
        _renderFrom(messages.slice(-HISTORY_RENDER_LIMIT));

        if (_older.length) {
            const more = document.createElement("button");
            more.className = "load-older-btn";
            more.textContent = `⤒ 加载更早的 ${_older.length} 条消息`;
            more.onclick = () => {
                more.remove();
                _renderFrom(_older);      // 渲染到最前面，顺序自然正确
                chat.scrollTop = 0;
            };
            chat.prepend(more);
        }
        attachHistoryRoundCards(sessionId, _roundEls);

        // 若切回的会话有"运行中的任务"：从内存重绘 + 重新挂载实时输出
        const st = sessionStates[sessionId];
        if (st && st.status === "running") {
            const restored = addAssistantMessage();
            restored.bubble.innerHTML =
                DOMPurify.sanitize(marked.parse(st.fullText || ""));
            restored.bubble.querySelectorAll('pre code').forEach(block => {
                hljs.highlightElement(block);
            });
            // 关键：把后台任务的 ctx 重新指向这块新 DOM，后续 SSE 继续写到这里
            if (st.ctx) Object.assign(st.ctx, restored);

            const hint = document.createElement("div");
            hint.className = "bubble session-running-hint";
            hint.style.cssText = "background:#f3f4f6;color:#6b7280;margin-top:6px;";
            hint.textContent = "⏳ 该会话仍在运行中，输出将实时更新…";
            restored.bubble.parentElement.appendChild(hint);
            // 记录引用：本会话后台任务结束时（runTask 的 finally）必须移除它，
            // 否则会话已跑完，这里仍残留"⏳ 该会话仍在运行中…"
            st.hintEl = hint;

            if (st.plan) renderPlan(st.plan);

            // 补渲染 pendingConfirm（缺口 1 修复）
            if (st.pendingConfirm) {
                const ev = st.pendingConfirm;
                restored.confirmToken = ev.confirm_token;   // 切回会话时恢复一次性令牌
                if (ev.type === "confirm_write") {
                    showWriteConfirm(restored, ev.path, ev.old_content, ev.new_content);
                } else if (ev.type === "confirm_delete") {
                    showDeleteConfirm(restored, ev.path, "");
                } else if (ev.type === "confirm_command") {
                    showCommandConfirm(restored, ev.cmd);
                }
                // 清掉，避免重复渲染
                delete st.pendingConfirm;
            }

            // 重绘 execHistory（缺口 2 修复）：切回后台运行中的会话时恢复执行卡片
            if (st.execHistory && st.execHistory.length > 0) {
                // 先清空执行详情区（restored 是本会话刚建的新 DOM）
                restored.execList.innerHTML = "";
                let replayResultEl = null;   // 等价于实时渲染里的 lastResultEl
                for (const hev of st.execHistory) {
                    if (hev.type === "tool_call") {
                        // 与实时渲染同款：写入 restored.execList
                        replayResultEl = appendToolCall(restored, hev.name, hev.args);
                    } else if (hev.type === "tool_result") {
                        if (replayResultEl) updateToolResult(replayResultEl, hev.content);
                    } else if (hev.type === "phase") {
                        // 与实时渲染同款：doing 建卡片，done 更新状态
                        if (hev.status === "doing") {
                            createPhaseCard(restored, hev.name);
                        } else {
                            updatePhaseStatus(restored, hev.name, hev.status);
                        }
                    } else if (hev.type === "plan_from_planner") {
                        // 与实时渲染同款：写进最近一张阶段卡片的 body
                        const card = restored.bubble.parentElement.querySelector(".phase-card:last-of-type .phase-body");
                        if (card) {
                            const steps = (hev.plan && hev.plan.steps) || [];
                            card.innerHTML = `
                                        <div style="font-weight:600;margin-bottom:6px;">📋 任务计划</div>
                                        <div class="planner-steps">
                                            ${steps.map((s, i) => `<div class="planner-step">${i + 1}. ${escapeHtml(s)}</div>`).join("")}
                                        </div>
                            `;
                        }
                    }
                }
            }
        }
    } catch (e) {
        console.error("加载历史失败", e);
    }

    // 按钮状态跟随"被切换到的会话"：A 在跑时切到 B，B 仍可发起新任务（多会话并行）
    // 「停止」按钮也在此同步：只有切到正在运行的会话才显示
    updateRunControls();
    loadPermission();   // 权限等级是「按会话」的，切会话要同步显示

    chat.scrollTop = chat.scrollHeight;
}

// 会话被删掉时顺手清掉用量记录（会话 id 是 AUTOINCREMENT 不复用，
// 不清也不会串号，只是别让 sessionUsage 无限长大）
function forgetUsage(sessionId) {
    delete sessionUsage[sessionId];
    delete sessionStates[sessionId];
}

async function createNewSession() {
    try {
        // 新会话建在「当前所选工作区」之下；没选就交给后端落到默认工作区
        const res = await fetch("/sessions", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ workspace_id: currentWorkspaceId }),
        });
        const data = await res.json();
        if (data.workspace_id && data.workspace_id !== currentWorkspaceId) {
            currentWorkspaceId = data.workspace_id;
            localStorage.setItem("currentWorkspaceId", String(data.workspace_id));
            renderWorkspaceSelector();
        }
        currentSessionId = data.id;
        await loadSessions();
        switchSession(data.id);
        // 会话数变了，顺手刷新工作区列表里的计数
        loadWorkspaces();
    } catch (e) {
        console.error("新建会话失败", e);
    }
}


// 旧的「手动输入项目路径 + 应用」已移除：路径由工作区决定（单一真相源）。
// 下方 loadProjectTree 直接取当前工作区的路径。

// ============ 项目树 ============
