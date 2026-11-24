// ===== 回收站（一键恢复）=====
async function loadTrashSummary() {
    const el = document.getElementById("trashSummary");
    if (!el) return;
    try {
        const res = await fetch("/api/trash");
        const j = await res.json();
        const n = (j.entries || []).length;
        el.textContent = n
            ? `共 ${n} 份备份（${(j.total_size / 1024).toFixed(1)} KB）`
            : "暂无备份";
    } catch (e) {
        el.textContent = "读取失败";
    }
}

function closeTrash() {
    const o = document.getElementById("trashOverlay");
    if (o) o.style.display = "none";
}

async function openTrash() {
    const o = document.getElementById("trashOverlay");
    if (o) o.style.display = "flex";
    await renderTrash();
}

// 当前选中的回收站条目 id（每次重新渲染都会清空）
let trashSelected = new Set();

function updateTrashSelectionUI() {
    const info = document.getElementById("trashSelInfo");
    const all = document.getElementById("trashSelectAll");
    const boxes = [...document.querySelectorAll(".trash-check")];
    const n = trashSelected.size;
    info.textContent = n ? `已选 ${n} 项` : "未选择";
    document.getElementById("trashBulkDelBtn").disabled = n === 0;
    document.getElementById("trashBulkRestoreBtn").disabled = n === 0;
    if (all) {
        all.checked = boxes.length > 0 && n === boxes.length;
        all.indeterminate = n > 0 && n < boxes.length;
    }
}

function toggleTrashSelectAll(checked) {
    trashSelected = checked
        ? new Set([...document.querySelectorAll(".trash-check")].map(b => b.dataset.id))
        : new Set();
    document.querySelectorAll(".trash-check").forEach(b => { b.checked = checked; });
    updateTrashSelectionUI();
}

async function renderTrash() {
    const list = document.getElementById("trashList");
    const sub = document.getElementById("trashSub");
    trashSelected = new Set();
    list.innerHTML = `<div class="trash-empty">加载中…</div>`;
    try {
        const res = await fetch("/api/trash");
        const j = await res.json();
        const entries = j.entries || [];
        sub.textContent = entries.length
            ? `共 ${entries.length} 份 · ${(j.total_size / 1024).toFixed(1)} KB`
            : "";
        if (!entries.length) {
            list.innerHTML = `<div class="trash-empty">回收站是空的</div>`;
            updateTrashSelectionUI();
            return;
        }
        // 用 data-* + 事件委托，避免把文件名拼进 onclick 造成注入
        list.innerHTML = entries.map(e => `
                    <div class="trash-item">
                        <input type="checkbox" class="trash-check" data-id="${escapeHtml(e.id)}"
                            title="选中以批量操作">
                        <span class="trash-kind">${escapeHtml(e.kind_label)}</span>
                        <div class="trash-meta">
                            <div class="trash-path" title="${escapeHtml(e.original_path || e.name)}">
                                ${escapeHtml(e.original_path || e.name)}
                            </div>
                            <div class="trash-info">${escapeHtml(e.mtime)} · ${(e.size / 1024).toFixed(1)} KB${e.restorable ? "" : " · 原始路径未知，无法一键恢复"}</div>
                        </div>
                        <button class="trash-btn restore" data-action="restore" data-id="${escapeHtml(e.id)}"
                            ${e.restorable ? "" : "disabled"}>恢复</button>
                        <button class="trash-btn" data-action="purge" data-id="${escapeHtml(e.id)}">彻底删除</button>
                    </div>`).join("");
        updateTrashSelectionUI();
    } catch (e) {
        list.innerHTML = `<div class="trash-empty">加载失败：${escapeHtml(e.message)}</div>`;
    }
}

async function restoreTrashEntry(id, btn) {
    if (!confirm("确定把这份备份恢复到它原来的路径吗？")) return;
    btn.disabled = true;
    btn.textContent = "恢复中…";
    try {
        const res = await fetch("/api/trash/restore", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ id }),
        });
        const j = await res.json();
        alert(j.message || (j.ok ? "已恢复" : "恢复失败"));
    } catch (e) {
        alert("恢复失败：" + e.message);
    }
    await renderTrash();
    await loadTrashSummary();
}

async function restoreSelectedTrash() {
    const ids = [...trashSelected];
    if (!ids.length) return;
    if (!confirm(`把选中的 ${ids.length} 条备份恢复到各自的原路径？`)) return;
    let ok = 0, fail = 0;
    for (const id of ids) {
        try {
            const res = await fetch("/api/trash/restore", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ id }),
            });
            (await res.json()).ok ? ok++ : fail++;
        } catch (e) { fail++; }
    }
    alert(`恢复完成：成功 ${ok} 条${fail ? `，失败 ${fail} 条` : ""}`);
    await renderTrash();
    await loadTrashSummary();
}

async function purgeTrashEntry(id, btn) {
    if (!confirm("彻底删除这条备份？此操作不可恢复。")) return;
    btn.disabled = true;
    btn.textContent = "删除中…";
    try {
        const res = await fetch(`/api/trash/${encodeURIComponent(id)}`, { method: "DELETE" });
        const j = await res.json();
        if (!j.ok) alert(j.message || "删除失败");
    } catch (e) {
        alert("删除失败：" + e.message);
    }
    await renderTrash();
    await loadTrashSummary();
}

async function purgeSelectedTrash() {
    const ids = [...trashSelected];
    if (!ids.length) return;
    if (!confirm(`彻底删除选中的 ${ids.length} 条备份？此操作不可恢复。`)) return;
    const btn = document.getElementById("trashBulkDelBtn");
    btn.disabled = true;
    btn.textContent = "删除中…";
    try {
        const res = await fetch("/api/trash/purge", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ids }),
        });
        const j = await res.json();
        alert(j.message || (j.ok ? "已删除" : "删除失败"));
    } catch (e) {
        alert("删除失败：" + e.message);
    }
    await renderTrash();
    await loadTrashSummary();
}

(function bindTrashActions() {
    const list = document.getElementById("trashList");
    if (!list) return;
    list.addEventListener("click", (ev) => {
        const btn = ev.target.closest("button[data-action]");
        if (!btn) return;
        const id = btn.dataset.id;
        if (btn.dataset.action === "restore") restoreTrashEntry(id, btn);
        else if (btn.dataset.action === "purge") purgeTrashEntry(id, btn);
    });
    // 勾选框用 change 事件（click 在键盘操作下不一定触发）
    list.addEventListener("change", (ev) => {
        const box = ev.target.closest(".trash-check");
        if (!box) return;
        box.checked ? trashSelected.add(box.dataset.id) : trashSelected.delete(box.dataset.id);
        updateTrashSelectionUI();
    });
})();

// ===== 任务中断 =====
// 依据「当前会话是否在运行」统一切换 执行/停止 按钮，
// 避免在多处各改一遍导致按钮状态不同步
function updateRunControls() {
    const runBtn = document.getElementById("runBtn");
    const stopBtn = document.getElementById("stopBtn");
    if (!runBtn || !stopBtn) return;
    const running = !!(currentSessionId
        && sessionStates[currentSessionId]
        && sessionStates[currentSessionId].status === "running");
    stopBtn.style.display = running ? "inline-block" : "none";
    stopBtn.disabled = false;
    stopBtn.textContent = "⏹ 停止";
    runBtn.disabled = running;
    runBtn.textContent = running ? "运行中..." : "执行";
}

async function cancelRun() {
    const sid = currentSessionId;
    if (!sid) return;
    const stopBtn = document.getElementById("stopBtn");
    if (stopBtn) {
        stopBtn.disabled = true;
        stopBtn.textContent = "停止中...";
    }
    try {
        const res = await fetch("/run/cancel", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ session_id: sid }),
        });
        const j = await res.json().catch(() => ({}));
        if (!j.cancelled) {
            // 没命中：任务可能刚好跑完（或本会话并未在运行）
            console.log("[CANCEL] 当前没有正在运行的任务");
            updateRunControls();
        }
    } catch (e) {
        console.warn("[CANCEL] 停止请求失败", e);
        updateRunControls();
    }
}

/** 正在发起的任务（防重入）。见下面 runTask 里的说明。 */
let taskStarting = false;

async function runTask() {
    // **显式防重入。**
    // 以前这里没有保护，靠的是「输入框在同步阶段就被清空」让第二次调用
    // 在下面的空输入判断处返回。那是**巧合不是设计**：
    // 当 currentSessionId 为空时，控制流会在 `await createNewSession()`
    // 处挂起，此时输入框还没清空，于是第二个调用又跑了一遍完整流程 ——
    // 实测会发出 2 次 /run 并建出 2 个会话。
    if (taskStarting) return;
    taskStarting = true;
    try {
        return await _runTaskInner();
    } finally {
        taskStarting = false;
    }
}

async function _runTaskInner() {
    const input = document.getElementById("task");
    const task = input.value.trim();
    const images = pendingImages.map(img => img.dataUrl);   // 待发送图片（data URL）
    // 文档附件只传路径+名字：正文在服务端读取后拼进消息，避免前端来回搬大文本
    const attachments = pendingAttachments.map(a => ({ path: a.path, name: a.name }));
    if (!task && !images.length && !attachments.length) return;
    if (!currentSessionId) {
        await createNewSession();
    }

    const mySessionId = currentSessionId;
    sessionStates[mySessionId] = { status: "running", fullText: "", plan: null, ctx: null };
    // 新的一轮开始：本会话用量继续累加（resetUsage 只在新建会话时调）
    loadSessions();   // 刷新会话列表，显示 ⏳

    addUserMessage(task, null, images, pendingAttachments.slice());
    clearImages();
    clearAttachments();
    input.value = "";
    updateRunControls();

    const ctx = addAssistantMessage();
    sessionStates[mySessionId].ctx = ctx;   // 供切回本会话时重新挂载实时输出
    ctx.sessionId = mySessionId;            // 标注 ctx 归属，供确认操作判断"用户是否仍停留在本会话"
    let fullText = "";   // 实际来源是 run.fullText，循环结束后回填

    try {
        const url = multiAgentMode ? "/run_multi" : "/run";
        const settings = {};  // 批次 4：前端不再配置 API，后端写死
        const body = {
            task: task || "[图片]",   // 仅图片、无文字时给个占位，避免后端判定任务为空
            session_id: currentSessionId,
            provider_id: currentProviderId || null,
            model: currentModelName || null,
            // 不再传 project_root：工作目录以「会话所属工作区」为准，
            // 由后端解析，避免前端全局 localStorage 覆盖其它会话的设置
        };
        if (images.length) body.images = images;   // 仅在有图片时才带，避免污染纯文本请求
        // 文档附件：只传 {path, name}，正文由后端读盘后拼进消息
        if (attachments.length) body.attachments = attachments;
        if (multiAgentMode) {
            // 多 Agent：三角色独立 provider + model
            body.provider_id = roleSelection.executor.providerId || currentProviderId;
            body.planner_provider_id = roleSelection.planner.providerId || currentProviderId;
            body.reviewer_provider_id = roleSelection.reviewer.providerId || currentProviderId;
            body.model = roleSelection.executor.model || currentModelName;
            body.planner_model = roleSelection.planner.model;
            body.executor_model = roleSelection.executor.model;
            body.reviewer_model = roleSelection.reviewer.model;
        }

        const res = await fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body)
        });
        const run = makeRunContext(mySessionId, ctx, task);
        await consumeSseStream(res, run);
        fullText = run.fullText;   // 流结束后要重新渲染，用统一的这份
        // 流结束后，重新渲染 + 高亮（仅当仍停留在本会话时才写 DOM）
        if (currentSessionId === mySessionId) {
            ctx.bubble.innerHTML = DOMPurify.sanitize(marked.parse(fullText));
            ctx.bubble.querySelectorAll('pre code').forEach(block => {
                hljs.highlightElement(block);
            });
        }
        await loadSessions();
    } catch (err) {
        if (currentSessionId === mySessionId) {
            ctx.bubble.textContent = "❌ 请求失败：" + err.message;
        }
    } finally {
        sessionStates[mySessionId].status = "done";
        // 本会话的"运行中"提示条若还在（用户停留/切回本会话时由 switchSession 注入），
        // 必须在这里移除，否则会话已结束、对话区仍显示"⏳ 该会话仍在运行中…"
        if (sessionStates[mySessionId].hintEl) {
            sessionStates[mySessionId].hintEl.remove();
            sessionStates[mySessionId].hintEl = null;
        }
        loadSessions();   // 刷新会话列表，去掉 ⏳（不论当前在哪个会话）
        updateRunControls();   // 任务结束：恢复「执行」、隐藏「停止」
        if (currentSessionId === mySessionId) {
            input.focus();
        }
    }
}

