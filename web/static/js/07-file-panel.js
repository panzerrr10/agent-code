// 一个 EditorView + 每个标签各一份 EditorState。
// state 自带文档与撤销栈，所以切回标签时撤销历史、光标都还在，
// 而且只占一个 DOM（开十几个文件也不重）。
let cmEditor = null;
let fpTabs = [];        // [{path, name, state, baseline, hash, eol, readOnly, size}]
let fpActive = -1;
const FP_MAX_TABS = 12;

function fpEl(id) { return document.getElementById(id); }
function filePanelOpen() { return fpEl("filePanel").style.display !== "none"; }
function fpCur() { return fpTabs[fpActive] || null; }

/** 标签当前文本：活动标签取编辑器实时内容，其它标签取它自己的 state。 */
function tabText(tab) {
    if (!tab) return "";
    if (tab === fpCur() && cmEditor) return cmEditor.getValue();
    return tab.state ? tab.state.doc.toString() : "";
}

function fpDirty(tab) {
    // diff 标签是只读快照，**永远不脏** —— 否则关面板/切工作区时会弹
    // 「有未保存改动」，而它根本没有可保存的东西。
    if (!tab || tab.kind === "diff") return false;
    return tabText(tab) !== tab.baseline;
}
function fpIsDirty() { return fpDirty(fpCur()); }

function setFilePanelVisible(visible) {
    fpEl("filePanel").style.display = visible ? "flex" : "none";
    fpEl("fileResizer").style.display = visible ? "block" : "none";
    if (visible) {
        applyFilePanelWidth();
        if (cmEditor) setTimeout(() => cmEditor.requestMeasure(), 0);
    }
    localStorage.setItem("filePanelOpen", visible ? "1" : "0");
}

function toggleFilePanel() {
    const visible = !filePanelOpen();
    setFilePanelVisible(visible);
    if (visible && !fpTabs.length) {
        showFileNotice("在左侧「项目树」里点文件即可打开；可同时打开多个，用顶部标签切换。");
    }
}

function closeFilePanel() {
    const dirty = fpTabs.filter(fpDirty);
    if (dirty.length
        && !confirm(`有 ${dirty.length} 个文件存在未保存的改动，确定关闭面板吗？`)) return;
    if (fpEl("filePanel").classList.contains("fullscreen")) toggleFileFullscreen();
    setFilePanelVisible(false);
}

function ensureEditor() {
    if (cmEditor) return cmEditor;
    if (!window.AgentCodeCM) {
        showFileNotice("CodeMirror 未加载（web/static/vendor/codemirror.min.js 缺失）", null, "error");
        return null;
    }
    cmEditor = window.AgentCodeCM.create(fpEl("fpEditor"), {
        doc: "",
        dark: document.body.classList.contains("dark"),
        onChange: updateFileButtons,
        onStateChange: updateFileButtons,
        onSave: () => saveFile(),
    });
    return cmEditor;
}

// ---- 标签栏 ----
function renderTabs() {
    const bar = fpEl("fpTabs");
    if (!bar) return;
    if (!fpTabs.length) { bar.style.display = "none"; bar.innerHTML = ""; return; }
    bar.style.display = "flex";
    bar.innerHTML = fpTabs.map((t, i) => `
                <span class="fp-tab ${i === fpActive ? "active" : ""} ${t.kind === "diff" ? "fp-tab-diff" : ""}" data-i="${i}"
                      title="${escapeHtml(t.title || t.path)}">
                    <span class="fp-tab-name">${t.kind === "diff" ? "± " : ""}${escapeHtml(t.name)}${fpDirty(t) ? '<b class="fp-tab-dot">●</b>' : ""}</span>
                    <button type="button" class="fp-tab-x" data-close="${i}" title="关闭">✕</button>
                </span>`).join("") + `<span class="fp-tab-count">${fpTabs.length}/${FP_MAX_TABS}</span>`;
}

(function bindTabBar() {
    const bar = fpEl("fpTabs");
    if (!bar) return;
    bar.addEventListener("click", (ev) => {
        const x = ev.target.closest("[data-close]");
        if (x) { ev.stopPropagation(); closeTab(Number(x.dataset.close)); return; }
        const tab = ev.target.closest(".fp-tab");
        if (tab) activateTab(Number(tab.dataset.i));
    });
    // 中键关闭（浏览器习惯）
    bar.addEventListener("auxclick", (ev) => {
        if (ev.button !== 1) return;
        const tab = ev.target.closest(".fp-tab");
        if (tab) { ev.preventDefault(); closeTab(Number(tab.dataset.i)); }
    });
})();

function activateTab(idx) {
    if (idx === fpActive || idx < 0 || idx >= fpTabs.length) return;
    const cur = fpCur();
    // 切换前把编辑器的当前 state 写回标签，否则这一份改动会丢
    if (cur && cmEditor) cur.state = cmEditor.getState();
    fpActive = idx;
    const tab = fpTabs[idx];
    // 只有「文件」标签才动 CodeMirror；diff 标签是另一套视图，
    // **两者互不干扰** —— 同一个文件可以同时开着「内容」和「本轮 diff」两个标签。
    if (tab && tab.kind !== "diff" && cmEditor) {
        cmEditor.setState(tab.state);
        cmEditor.setTheme(document.body.classList.contains("dark"));
    }
    applyActiveTabView();
    renderTabs();
    renderFileHeader();
    updateFileButtons();
    if (cmEditor && tab && tab.kind !== "diff" && !tab.readOnly) cmEditor.focus();
    if (cmEditor) setTimeout(() => cmEditor.requestMeasure(), 0);
}

function closeTab(idx) {
    const tab = fpTabs[idx];
    if (!tab) return;
    if (fpDirty(tab) && !confirm(`「${tab.name}」有未保存的改动，确定关闭吗？`)) return;
    fpTabs.splice(idx, 1);
    if (!fpTabs.length) {
        fpActive = -1;
        if (cmEditor) cmEditor.setValue("");
        fpEl("fpTabs").style.display = "none";
        fpEl("fpName").textContent = "未打开文件";
        fpEl("fpPath").textContent = "";
        fpEl("fpInfo").textContent = "";
        clearFileNotice();
        updateFileButtons();
        persistTabs();
        return;
    }
    fpActive = Math.max(0, Math.min(idx, fpTabs.length - 1));
    const next = fpTabs[fpActive];
    if (cmEditor) { cmEditor.setState(next.state); }
    renderTabs();
    renderFileHeader();
    updateFileButtons();
    persistTabs();
}

/** 按当前标签类型切换编辑器 / diff 视图。 */
function applyActiveTabView() {
    const tab = fpCur();
    const isDiff = !!tab && tab.kind === "diff";
    const ed = fpEl("fpEditor");
    const df = fpEl("fpDiff");
    if (ed) ed.style.display = isDiff ? "none" : "";
    if (df) {
        df.style.display = isDiff ? "block" : "none";
        if (isDiff) {
            df.innerHTML = tab.diffHtml || "";
            df.scrollTop = 0;
        }
    }
    if (!isDiff && cmEditor) setTimeout(() => cmEditor.requestMeasure(), 0);
}

/**
 * 渲染后端给的 unified diff 文本（只读）。
 *
 * **刻意不用前端的 generateDiff** —— 那个是朴素 LCS，DP 表 O(m×n)，
 * 几千行的文件就是几千万格、会把浏览器卡死。文件级 diff 一律后端算。
 */
function renderUnifiedDiff(text) {
    const lines = String(text || "").split("\n");
    if (!lines.length || !String(text || "").trim()) {
        return '<div class="fp-diff-empty">（没有可显示的差异）</div>';
    }
    let html = "";
    for (const ln of lines) {
        if (ln.startsWith("--- ") || ln.startsWith("+++ ")) continue;  // 文件头，没用
        if (ln.startsWith("@@")) {
            html += `<span class="diff-line diff-hunk">${escapeHtml(ln)}</span>`;
        } else if (ln.startsWith("+")) {
            html += `<span class="diff-line diff-add">${escapeHtml(ln)}</span>`;
        } else if (ln.startsWith("-")) {
            html += `<span class="diff-line diff-del">${escapeHtml(ln)}</span>`;
        } else {
            html += `<span class="diff-line diff-unchanged">${escapeHtml(ln)}</span>`;
        }
    }
    return html;
}

/**
 * 打开「本轮改动」的 diff（**与点项目树打开文件是两条独立的路**）。
 *
 * - 点项目树 → openFile() → kind:"file" → CodeMirror 可编辑
 * - 点改动文件 → openDiff() → kind:"diff" → 只读 diff
 * 两条路各自建标签，同一个文件可以同时开两个，互不影响。
 */
async function openDiff(sessionId, runIndex, path) {
    setFilePanelVisible(true);
    const sid = sessionId != null ? sessionId : currentSessionId;
    const key = `diff:${sid}:${runIndex}:${path}`;

    const existing = fpTabs.findIndex(t => t.key === key);
    if (existing >= 0) { activateTab(existing); return; }

    if (fpTabs.length >= FP_MAX_TABS) {
        showFileNotice(`最多同时打开 ${FP_MAX_TABS} 个标签，请先关掉一些。`, null, "warn");
        return;
    }

    let data;
    try {
        const res = await fetch(`/api/run_change_diff?session_id=${sid}`
            + `&run_index=${runIndex}&path=${encodeURIComponent(path)}`);
        data = await res.json();
    } catch (e) {
        showFileNotice("读取改动失败：" + e.message, null, "error");
        return;
    }
    if (!data.ok) { showFileNotice(data.error || "读取改动失败", null, "error"); return; }

    const name = String(path).split("/").pop();
    const truncatedNote = data.truncated
        ? '<div class="fp-diff-note">⚠️ 改动过大，仅显示前一部分</div>' : "";
    fpTabs.push({
        kind: "diff", key,
        path, name,
        title: `第 ${runIndex} 轮改动 · ${name}`,
        runIndex, sessionId: sid,
        diffHtml: truncatedNote + renderUnifiedDiff(data.diff),
        added: data.added, removed: data.removed,
        changeType: data.change_type,
    });
    fpActive = fpTabs.length - 1;
    applyActiveTabView();
    renderTabs();
    renderFileHeader();
    updateFileButtons();
    clearFileNotice();
}

function renderFileHeader() {
    const tab = fpCur();
    if (tab && tab.kind === "diff") {
        fpEl("fpName").textContent = `第 ${tab.runIndex} 轮改动 · ${tab.name}`;
        fpEl("fpPath").textContent = tab.path;
        fpEl("fpPath").title = tab.path;
        const badge = { create: "新建", delete: "已删除", modify: "修改" }[tab.changeType] || "";
        fpEl("fpInfo").textContent = `${badge} · 只读对比 · +${tab.added} −${tab.removed}`;
        return;
    }
    fpEl("fpName").textContent = tab ? tab.name : "未打开文件";
    fpEl("fpPath").textContent = tab ? tab.path : "";
    fpEl("fpPath").title = tab ? tab.path : "";
    if (tab) {
        const size = tab.size < 1024
            ? `${tab.size} B` : `${(tab.size / 1024).toFixed(1)} KB`;
        const enc = tab.readOnly ? "只读" : "UTF-8";
        fpEl("fpInfo").textContent = `${enc} · ${String(tab.eol).toUpperCase()} · ${size}`;
    }
}

// ===== 文件标签按工作区隔离 =====
// 两层：
//   内存 wsTabCache —— 保留完整标签对象（含**未保存的编辑**），切工作区秒切且不丢改动
//   服务端 open_files —— 只存路径列表，重启/换浏览器后按路径重新打开
let wsTabCache = {};        // { [wsId]: { tabs: [...], active: n } }
let wsPersistTimer = null;

function snapshotTabs() {
    if (currentWorkspaceId == null) return;
    // **必须先把编辑器的实时 state 写回活动标签**：
    // CodeMirror 的 state 是不可变的，dispatch 之后产生的是新对象，
    // 标签里存的还是载入时那一份。不写回的话，切走工作区就丢了未保存的编辑。
    const tab = fpCur();
    if (tab && cmEditor) tab.state = cmEditor.getState();
    wsTabCache[currentWorkspaceId] = { tabs: fpTabs, active: fpActive };
}

function persistTabs() {
    if (currentWorkspaceId == null) return;
    clearTimeout(wsPersistTimer);
    // 防抖：连续开关标签/切工作区时不必每次都打服务端
    wsPersistTimer = setTimeout(() => {
        const wsId = currentWorkspaceId;
        // 只持久化「文件」标签。diff 标签是某一轮的快照，
        // 下次打开早就过期了，存下来只会在重启后弹出一堆无效标签。
        const paths = fpTabs.filter(t => t.kind !== "diff").map(t => t.path);
        fetch(`/workspaces/${wsId}/open_files`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ paths }),
        }).catch(e => console.warn("[tabs] 保存打开记录失败", e));
    }, 600);
}

/** 把面板整体换成某个工作区的标签集合（内存里已有）。 */
function applyTabsForWorkspace(wsId) {
    const cached = wsTabCache[wsId];
    fpTabs = cached ? cached.tabs || [] : [];
    fpActive = cached ? cached.active : -1;
    if (fpActive < 0 || fpActive >= fpTabs.length) fpActive = fpTabs.length ? 0 : -1;

    if (cmEditor) {
        const tab = fpCur();
        if (tab) {
            cmEditor.setState(tab.state);
            cmEditor.setTheme(document.body.classList.contains("dark"));
        } else {
            cmEditor.setState(cmEditor.createState("", "plain", false));
        }
    }
    renderTabs();
    renderFileHeader();
    updateFileButtons();
    if (fpTabs.length) {
        setFilePanelVisible(true);
        clearFileNotice();
        if (cmEditor) setTimeout(() => cmEditor.requestMeasure(), 0);
    } else {
        showFileNotice("此工作区还没有打开文件；在左侧「项目树」里点文件即可打开。");
    }
}

/** 内存里没有该工作区 → 按服务端记录的路径重新打开。 */
async function restoreTabsForWorkspace(wsId) {
    fpTabs = [];
    fpActive = -1;
    if (cmEditor) cmEditor.setState(cmEditor.createState("", "plain", false));
    renderTabs();
    renderFileHeader();
    updateFileButtons();

    let paths = [];
    try {
        const res = await fetch(`/workspaces/${wsId}/open_files`);
        const j = await res.json();
        if (j.ok) paths = j.paths || [];
    } catch (e) {
        console.warn("[tabs] 读取打开记录失败", e);
    }
    if (!paths.length) {
        showFileNotice("此工作区还没有打开文件；在左侧「项目树」里点文件即可打开。");
        return;
    }
    for (const path of paths.slice(0, FP_MAX_TABS)) {
        await openFile(path);   // 文件已被删时 openFile 会提示，不阻塞其它标签
    }
    if (fpTabs.length) clearFileNotice();
}

/**
 * 切工作区时换一套标签。
 *
 * **调用前 currentWorkspaceId 必须已经是新工作区**，快照由 selectWorkspace
 * 用「旧的」id 完成 —— 之前这里又 snapshot 了一次，而那时 currentWorkspaceId
 * 已经改成新工作区，结果把旧工作区的标签错存成了新工作区的，表现为
 * 「切到 B 区后 A 区的文件还在」。
 */
async function switchWorkspaceTabs(newWsId) {
    if (wsTabCache[newWsId]) applyTabsForWorkspace(newWsId);
    else await restoreTabsForWorkspace(newWsId);
}

// ---- 打开 / 保存 ----
async function openFile(path) {
    setFilePanelVisible(true);

    // 只在「文件」标签里查重 —— diff 标签虽然路径相同，但那是另一回事
    const existing = fpTabs.findIndex(t => t.kind !== "diff" && t.path === path);
    if (existing >= 0) { activateTab(existing); return; }

    if (fpTabs.length >= FP_MAX_TABS) {
        showFileNotice(`最多同时打开 ${FP_MAX_TABS} 个文件，请先关掉一些标签。`, null, "warn");
        return;
    }
    if (!ensureEditor()) return;

    let data;
    try {
        const res = await fetch(`/api/file_content?path=${encodeURIComponent(path)}`);
        data = await res.json();
    } catch (e) {
        showFileNotice("打开失败：" + e.message, null, "error");
        return;
    }
    if (!data.ok) { showFileNotice(data.error || "打开失败", null, "error"); return; }

    const name = String(data.path).split("/").pop();
    const content = data.editable ? data.content : "";
    const state = cmEditor.createState(
        content, window.AgentCodeCM.detectLang(data.path), !data.editable);

    fpTabs.push({
        kind: "file", key: `file:${data.path}`,
        path: data.path, name, state, baseline: content,
        hash: data.hash, eol: data.eol || "lf", readOnly: !data.editable,
        size: data.size, message: data.message || "",
    });
    fpActive = fpTabs.length - 1;
    cmEditor.setState(state);
    cmEditor.setTheme(document.body.classList.contains("dark"));
    applyActiveTabView();
    renderTabs();
    renderFileHeader();
    updateFileButtons();
    persistTabs();

    if (data.editable) {
        clearFileNotice();
        cmEditor.focus();
    } else {
        showFileNotice("⛔ " + (data.message || "该文件不可编辑"), null, "warn");
        if (cmEditor) cmEditor.requestMeasure();
    }
}

function updateFileButtons() {
    const tab = fpCur();
    const isDiff = !!tab && tab.kind === "diff";
    const dirty = fpIsDirty();
    const save = fpEl("fpSaveBtn");
    if (save) {
        save.disabled = isDiff || !dirty || (tab && tab.readOnly);
        save.textContent = dirty ? "💾 保存 *" : "💾 保存";
    }
    const dirtyTag = fpEl("fpDirty");
    if (dirtyTag) dirtyTag.style.display = dirty ? "inline" : "none";
    // diff 是只读快照，撤销/重做/让 Agent 处理都无意义
    if (fpEl("fpUndoBtn")) fpEl("fpUndoBtn").disabled = isDiff || !cmEditor || !cmEditor.canUndo();
    if (fpEl("fpRedoBtn")) fpEl("fpRedoBtn").disabled = isDiff || !cmEditor || !cmEditor.canRedo();
    if (fpEl("fpAskBtn")) fpEl("fpAskBtn").disabled = !tab || isDiff;
    renderTabs();   // 脏标记要跟着更新
}

/** 面板底部提示条。actions 形如 [{label, act, danger}] */
function showFileNotice(text, actions, kind) {
    const box = fpEl("fpNotice");
    box.className = "fp-notice" + (kind ? " " + kind : "");
    box.innerHTML = `<div>${escapeHtml(text)}</div>`;
    if (actions && actions.length) {
        const row = document.createElement("div");
        row.className = "fp-notice-actions";
        actions.forEach(a => {
            const b = document.createElement("button");
            b.className = "fp-btn" + (a.danger ? " danger" : "");
            b.textContent = a.label;
            b.onclick = a.act;
            row.appendChild(b);
        });
        box.appendChild(row);
    }
    box.style.display = "block";
}

function clearFileNotice() { fpEl("fpNotice").style.display = "none"; }

async function saveFile(force) {
    const tab = fpCur();
    if (!tab || !cmEditor) return;
    if (tab.readOnly) { showFileNotice("该文件为只读，无法保存。", null, "warn"); return; }
    if (!fpIsDirty()) { clearFileNotice(); return; }

    const body = { path: tab.path, content: cmEditor.getValue(), eol: tab.eol };
    if (!force) body.expected_hash = tab.hash;

    let res, data;
    try {
        res = await fetch("/api/file_content", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        data = await res.json();
    } catch (e) {
        showFileNotice("保存失败：" + e.message, null, "error");
        return;
    }

    if (res.status === 409 && data.error === "stale") {
        // 磁盘内容被别人（多半是 Agent）改过了 —— 绝不静默覆盖
        showFileNotice(
            "⚠️ 文件在磁盘上已被改动（可能是 Agent 刚写过）。请选择怎么处理：",
            [
                { label: "重新加载磁盘内容", act: () => reloadTab(tab.path) },
                { label: "强制覆盖（用我的版本）", act: () => saveFile(true), danger: true },
            ],
            "warn"
        );
        return;
    }
    if (!data.ok) { showFileNotice(data.error || "保存失败", null, "error"); return; }

    tab.hash = data.hash;
    tab.baseline = cmEditor.getValue();
    tab.state = cmEditor.getState();
    updateFileButtons();
    showFileNotice(
        data.backup ? `✅ 已保存（改动前的版本已进回收站：${data.backup}）` : "✅ 已保存",
        [{ label: "知道了", act: clearFileNotice }]
    );
    loadTrashSummary();
}

/** 冲突时选「重新加载」：把该标签的内容换成磁盘上的最新版。 */
async function reloadTab(path) {
    const idx = fpTabs.findIndex(t => t.path === path);
    if (idx < 0) return;
    try {
        const res = await fetch(`/api/file_content?path=${encodeURIComponent(path)}`);
        const data = await res.json();
        if (!data.ok) { showFileNotice(data.error || "重新加载失败", null, "error"); return; }
        const tab = fpTabs[idx];
        const content = data.editable ? data.content : "";
        tab.state = cmEditor.createState(
            content, window.AgentCodeCM.detectLang(path), !data.editable);
        tab.baseline = content;
        tab.hash = data.hash;
        tab.eol = data.eol || "lf";
        tab.readOnly = !data.editable;
        tab.size = data.size;
        if (idx === fpActive) { cmEditor.setState(tab.state); }
        renderTabs(); renderFileHeader(); updateFileButtons();
        showFileNotice("已重新加载磁盘上的最新内容。");
    } catch (e) {
        showFileNotice("重新加载失败：" + e.message, null, "error");
    }
}

function undoFile() { if (cmEditor) { cmEditor.focus(); cmEditor.undo(); updateFileButtons(); } }
function redoFile() { if (cmEditor) { cmEditor.focus(); cmEditor.redo(); updateFileButtons(); } }

function toggleFileFullscreen() {
    const panel = fpEl("filePanel");
    const on = panel.classList.toggle("fullscreen");
    const btn = fpEl("fpFullBtn");
    if (btn) {
        btn.textContent = on ? "⤡" : "⛶";
        btn.title = on ? "退出全屏（Esc）" : "全屏显示（Esc 退出）";
    }
    if (cmEditor) setTimeout(() => cmEditor.requestMeasure(), 0);
}

/** 下载当前标签对应的文件。 */
function downloadCurrentFile() {
    const tab = fpCur();
    if (!tab) { showFileNotice("还没有打开文件。", null, "warn"); return; }
    downloadPath(tab.path, tab.name);
}

/** 保留原来「点文件让 Agent 读」的能力，改成显式按钮触发。 */
function askAgentAboutFile() {
    const tab = fpCur();
    if (!tab) { showFileNotice("还没有打开文件。", null, "warn"); return; }
    const input = document.getElementById("task");
    input.value = `读取文件 ${tab.path} 并总结内容`;
    input.focus();
    showFileNotice("已填入对话输入框，按「执行」交给 Agent。");
}

// ---- 面板宽度：拖动调整 + 记忆 ----
function applyFilePanelWidth() {
    const saved = parseInt(localStorage.getItem("filePanelWidth") || "", 10);
    if (saved >= 280) fpEl("filePanel").style.width = saved + "px";
}

(function bindFileResizer() {
    const bar = fpEl("fileResizer");
    const panel = fpEl("filePanel");
    if (!bar || !panel) return;
    let dragging = false, startX = 0, startW = 0;
    bar.addEventListener("mousedown", (e) => {
        dragging = true;
        startX = e.clientX;
        startW = panel.getBoundingClientRect().width;
        bar.classList.add("dragging");
        document.body.style.cursor = "col-resize";
        document.body.style.userSelect = "none";
        e.preventDefault();
    });
    document.addEventListener("mousemove", (e) => {
        if (!dragging) return;
        const w = Math.min(Math.max(startW - (e.clientX - startX), 280), window.innerWidth * 0.8);
        panel.style.width = w + "px";
    });
    document.addEventListener("mouseup", () => {
        if (!dragging) return;
        dragging = false;
        bar.classList.remove("dragging");
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
        localStorage.setItem("filePanelWidth", String(panel.getBoundingClientRect().width | 0));
        if (cmEditor) cmEditor.requestMeasure();
    });
})();

// ---- 快捷键（capture 阶段，确保 CodeMirror 聚焦时也生效）----
document.addEventListener("keydown", (e) => {
    const mod = e.ctrlKey || e.metaKey;
    if (mod && !e.altKey) {
        const k = e.key.toLowerCase();
        if (k === "b") {                       // 显示 / 隐藏文件面板
            e.preventDefault(); e.stopPropagation(); toggleFilePanel(); return;
        }
        if (k === "s") {                       // 保存（阻止浏览器「保存网页」）
            if (filePanelOpen()) { e.preventDefault(); e.stopPropagation(); saveFile(); }
            return;
        }
    }
    if (e.key === "Escape") {
        const sm = document.getElementById("settingsModal");
        if (sm && sm.style.display !== "none") { e.preventDefault(); closeSettings(); return; }
        const panel = fpEl("filePanel");
        if (panel && panel.classList.contains("fullscreen")) {
            e.preventDefault(); toggleFileFullscreen();
        }
    }
}, true);

// 启动时先恢复面板的显示状态；**具体标签由 bootstrap 按当前工作区恢复**
// （标签是「按工作区」的，必须等工作区确定后才知道该开哪些）
(function restoreFilePanelVisibility() {
    if (localStorage.getItem("filePanelOpen") === "1") setFilePanelVisible(true);
})();

// ============ 图片上传 / 粘贴（多模态）============
