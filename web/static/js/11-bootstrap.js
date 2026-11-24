// 启动顺序很重要：先确定工作区，再按工作区载入会话列表与项目树
(async function bootstrap() {
    await loadWorkspaces();                                   // 先定下当前工作区
    if (currentWorkspaceId != null) {
        await restoreTabsForWorkspace(currentWorkspaceId);    // 再恢复该工作区打开过的文件
    }
    await loadSessions();
    await loadProjectTree();
})();
loadDocuments();
loadTrashSummary();

// 关掉整个应用前提醒未保存的编辑（切工作区不会丢，它在内存缓存里）
window.addEventListener("beforeunload", (e) => {
    const dirty = fpTabs.filter(fpDirty).length;
    if (dirty > 0) {
        e.preventDefault();
        e.returnValue = `有 ${dirty} 个文件未保存`;
        return e.returnValue;
    }
});

// 支持用 #trash 直接打开回收站（方便做书签或桌面快捷方式）
if (location.hash === "#trash") {
    openTrash();
}
// ===== 输入框自动长高 =====
const inputEl = document.getElementById("task");

function autoResizeInput() {
    inputEl.style.height = "auto";
    inputEl.style.height = Math.min(inputEl.scrollHeight, 300) + "px";
}

inputEl.addEventListener("input", autoResizeInput);

// 初始化高度
autoResizeInput();

// Enter 发送，Shift+Enter 换行
inputEl.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        runTask();
    }
});

// ===== 任务计划面板 =====
function renderPlan(plan) {
    const panel = document.getElementById("planPanel");
    const list = document.getElementById("planList");

    if (!plan || plan.length === 0) {
        panel.style.display = "none";
        return;
    }

    panel.style.display = "block";
    list.innerHTML = "";

    plan.forEach((item, idx) => {
        const status = item.status || "pending";
        const iconMap = {
            "pending": "○",
            "doing": "⏳",
            "done": "✅",
            "failed": "❌",
        };
        const icon = iconMap[status] || "○";

        const div = document.createElement("div");
        div.className = "plan-item " + status;
        div.innerHTML = `
            <span class="plan-icon">${icon}</span>
            <span class="plan-text">${idx + 1}. ${escapeHtml(item.step)}</span>
`;
        list.appendChild(div);
    });
}

function togglePlan() {
    const list = document.getElementById("planList");
    const btn = document.querySelector(".plan-toggle");
    if (list.style.display === "none") {
        list.style.display = "flex";
        btn.textContent = "收起";
    } else {
        list.style.display = "none";
        btn.textContent = "展开";
    }
}
function showCommandConfirm(ctx, cmd) {
    const existing = ctx.bubble.parentElement.querySelector(".confirm-actions");
    if (existing) existing.remove();

    const div = document.createElement("div");
    div.className = "confirm-actions";
    div.style.flexDirection = "column";
    div.style.alignItems = "stretch";
    div.innerHTML = `
                <div style="display:flex;align-items:center;gap:8px;">
                    <span class="confirm-text" style="flex:1;">⚡ 确认执行命令？</span>
                    <button class="confirm-btn yes" title="执行">✓</button>
                    <button class="confirm-btn no" title="取消">✗</button>
                </div>
                <pre style="margin-top:6px;padding:8px;background:#1e293b;color:#e2e8f0;border-radius:6px;font-size:12px;white-space:pre-wrap;word-break:break-all;">${escapeHtml(cmd)}</pre>
    `;

    div.querySelector(".confirm-btn.yes").addEventListener("click", () => {
        div.remove();
        // 用户确认后：续跑 Agent（复用 pending state，从原 step 继续）
        resumeRun(ctx);
    });

    div.querySelector(".confirm-btn.no").addEventListener("click", () => {
        div.remove();
        const cancelMsg = document.createElement("div");
        cancelMsg.className = "bubble";
        cancelMsg.style.background = "#f3f4f6";
        cancelMsg.style.color = "#6b7280";
        cancelMsg.style.marginTop = "6px";
        cancelMsg.textContent = "❌ 已取消执行";
        ctx.bubble.parentElement.appendChild(cancelMsg);
    });

    ctx.bubble.parentElement.appendChild(div);
    chat.scrollTop = chat.scrollHeight;
}

// 切换会话时清空计划面板
function clearPlan() {
    document.getElementById("planPanel").style.display = "none";
}
function applyTheme(isDark) {
    document.body.classList.toggle("dark", isDark);
    const btn = document.getElementById("themeBtn");
    if (btn) btn.textContent = isDark ? "☀️" : "🌙";
    localStorage.setItem("theme", isDark ? "dark" : "light");
    // 右侧文件面板里的 CodeMirror 也要跟着换主题
    if (cmEditor) cmEditor.setTheme(isDark);
}
function toggleTheme() {
    const isDark = !document.body.classList.contains("dark");
    applyTheme(isDark);
}
(function initTheme() {
    const saved = localStorage.getItem("theme");
    if (saved === "dark") {
        applyTheme(true);
    }
})();

// ===== 文档管理 =====
async function loadDocuments() {
    try {
        const res = await fetch("/documents");
        const docs = await res.json();
        renderDocList(docs);
    } catch (e) {
        console.error("加载文档失败", e);
    }
}

function renderDocList(docs) {
    const list = document.getElementById("docList");
    list.innerHTML = "";

    if (!docs || docs.length === 0) {
        list.innerHTML = '<div class="doc-empty">暂无文档</div>';
        return;
    }

    docs.forEach(d => {
        const div = document.createElement("div");
        div.className = "doc-item";
        div.innerHTML = `
                    <span class="doc-name" title="${escapeHtml(d.filename)}">${escapeHtml(d.filename)}</span>
                    <span class="doc-chunks">${d.chunk_count}段</span>
                    <button class="doc-delete" title="删除">✕</button>
        `;

        div.querySelector(".doc-delete").addEventListener("click", async () => {
            if (!confirm(`确定删除文档「${d.filename}」吗？`)) return;
            await fetch(`/documents/${d.id}`, { method: "DELETE" });
            await loadDocuments();
        });

        list.appendChild(div);
    });
}

function triggerUpload() {
    document.getElementById("fileInput").click();
}

async function handleFileUpload(event) {
    const file = event.target.files[0];
    if (!file) return;

    if (file.size > 20 * 1024 * 1024) {
        alert("文件太大，请上传 5MB 以内的文件");
        event.target.value = "";
        return;
    }

    const formData = new FormData();
    formData.append("file", file);

    const docList = document.getElementById("docList");
    const loading = document.createElement("div");
    loading.className = "doc-empty";
    loading.textContent = `📤 上传中：${file.name}...`;
    docList.appendChild(loading);

    try {
        const res = await fetch("/upload_doc", {
            method: "POST",
            body: formData
        });
        const data = await res.json();

        if (data.error) {
            alert("上传失败：" + data.error);
        }
    } catch (e) {
        alert("上传出错：" + e.message);
    } finally {
        event.target.value = "";
        await loadDocuments();
    }
}
// ===== 消息操作 =====
function editMessage(btn) {
    const msgDiv = btn.closest(".msg");
    const raw = msgDiv.dataset.rawText || "";
    const input = document.getElementById("task");
    input.value = raw;
    input.focus();
    // 删掉这条消息及后面的所有消息
    let next = msgDiv.nextElementSibling;
    while (next) {
        const toRemove = next;
        next = next.nextElementSibling;
        toRemove.remove();
    }
    msgDiv.remove();
}

function regenerate(btn) {
    // 找到上一条用户消息
    const msgDiv = btn.closest(".msg");
    let prev = msgDiv.previousElementSibling;
    let userMsg = null;
    while (prev) {
        if (prev.classList.contains("user")) {
            userMsg = prev.dataset.rawText;
            break;
        }
        prev = prev.previousElementSibling;
    }
    if (!userMsg) {
        alert("找不到对应的用户问题");
        return;
    }
    // 删掉这条 AI 消息
    msgDiv.remove();
    // 重新跑
    const input = document.getElementById("task");
    input.value = userMsg;
    runTask();
}
function nowTime() {
    const d = new Date();
    const pad = n => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function copyMessage(btn) {
    const bubble = btn.closest(".bubble-wrap").querySelector(".bubble");
    navigator.clipboard.writeText(bubble.innerText).then(() => {
        btn.textContent = "✅";
        setTimeout(() => { btn.textContent = "📋"; }, 1500);
    });
}

// ===== 模式切换 =====
function setMode(mode) {
    multiAgentMode = (mode === "multi");
    document.querySelectorAll(".mode-btn").forEach(btn => {
        btn.classList.toggle("active", btn.dataset.mode === mode);
    });
    localStorage.setItem("agentMode", mode);
    // 三角色选择器现在固定在设置面板里，始终可见（不再随模式隐藏）
    initRoleSelection();
}

// 页面加载时恢复模式
(function initMode() {
    const saved = localStorage.getItem("agentMode") || "single";
    setMode(saved);
})();
// ===== 多 Agent 阶段卡片 =====
function createPhaseCard(ctx, phaseName) {
    const card = document.createElement("div");
    card.className = "phase-card";

    const headerText = {
        "planner": "🧠 Planner（规划）",
        "executor": "⚙️ Executor（执行）",
        "reviewer": "🔍 Reviewer（审查）",
    }[phaseName] || phaseName;

    card.innerHTML = `
                <div class="phase-header ${phaseName}">
                    <span>${headerText}</span>
                    <span class="phase-status" id="status-${phaseName}">进行中...</span>
                </div>
                <div class="phase-body"></div>
    `;

    // 插到 bubble 前面
    ctx.bubble.parentElement.insertBefore(card, ctx.bubble);
    followChat();
    return card;
}

function updatePhaseStatus(ctx, phaseName, status) {
    const statusEl = ctx.bubble.parentElement.querySelector(`#status-${phaseName}`);
    if (statusEl) {
        if (status === "doing") {
            statusEl.textContent = "进行中...";
        } else if (status === "done") {
            statusEl.textContent = "✅ 完成";
        }
    }
}

function toggleExportMenu(e) {
    e.stopPropagation();
    const menu = document.getElementById("exportMenu");
    menu.style.display = menu.style.display === "none" ? "block" : "none";
}

function exportAs(fmt) {
    if (!currentSessionId) {
        alert("当前没有选中的会话");
        return;
    }
    downloadExport(fmt, "对话记录");
    document.getElementById("exportMenu").style.display = "none";
}

// 点击别处关闭菜单
document.addEventListener("click", () => {
    const menu = document.getElementById("exportMenu");
    if (menu) menu.style.display = "none";
});
