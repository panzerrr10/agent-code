const MAX_IMAGE_BYTES = 10 * 1024 * 1024;   // 单张上限 10MB
let pendingImages = [];   // [{ dataUrl, name }]

// 将图片文件压缩到最长边 maxSize 像素，返回 data URL
function compressImage(file, maxSize = 1024, quality = 0.85) {
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onerror = () => reject(new Error("读取图片失败"));
        reader.onload = e => {
            const img = new Image();
            img.onerror = () => reject(new Error("图片解析失败"));
            img.onload = () => {
                let w = img.width, h = img.height;
                const longest = Math.max(w, h);
                // 小于 maxSize 则不放大
                if (longest <= maxSize) {
                    resolve(e.target.result);
                    return;
                }
                const scale = maxSize / longest;
                const nw = Math.round(w * scale);
                const nh = Math.round(h * scale);
                const canvas = document.createElement("canvas");
                canvas.width = nw;
                canvas.height = nh;
                const ctx = canvas.getContext("2d");
                // 白底填充（防止 PNG 透明背景变黑）
                ctx.fillStyle = "#ffffff";
                ctx.fillRect(0, 0, nw, nh);
                ctx.drawImage(img, 0, 0, nw, nh);
                // 统一输出 JPEG（体积最小）
                const dataUrl = canvas.toDataURL("image/jpeg", quality);
                console.log(`[IMG] 压缩: ${file.name} ${(file.size/1024).toFixed(0)}KB → ${(dataUrl.length*0.75/1024).toFixed(0)}KB`);
                resolve(dataUrl);
            };
            img.src = e.target.result;
        };
        reader.readAsDataURL(file);
    });
}

// ===== 设置面板：左侧导航分页 =====
const SETTINGS_PAGES = ["general", "keys", "agents", "mcp", "kb"];
let currentSettingsPage = "general";

// 用户配置的模型提供商。**后端没有预设**，这几个变量是它们在前端的唯一来源。
// 待发送的附件（回形针按钮加进来的），必须在 runTask 之前声明
let pendingAttachments = [];   // [{name, path, size, chars}]

let providersData = [];      // 列表（不含完整密钥，只有 has_key / key_hint）
let providersDefault = "";   // 默认提供商的 id
let providersRag = "";       // 知识库（向量检索）用哪家
let currentProviderId = "";  // 当前选中的提供商
let currentModelName = "";   // 当前选中的模型
let currentPermission = 2;   // 当前会话的权限等级
let tokens = null;           // 最近一次的 token 用量（历史回显用）

function showSettingsPage(name) {
    if (!SETTINGS_PAGES.includes(name)) name = "general";
    currentSettingsPage = name;
    document.querySelectorAll("#settingsNav .snav-item").forEach(b => {
        b.classList.toggle("active", b.dataset.page === name);
    });
    document.querySelectorAll("#settingsModal .spage").forEach(pg => {
        pg.classList.toggle("active", pg.dataset.page === name);
    });
    // 切到哪一页就只加载那一页的数据（不必每次开设置都拉全部）
    if (name === "kb") loadDocuments();
    if (name === "mcp") loadMcpServers();
    if (name === "agents") loadProjectContext();
    if (name === "keys") renderProviderKeys();
}

// ===== 设置面板 =====
function openSettings(page) {
    document.getElementById("settingsModal").style.display = "flex";
    if (!providersData.length) loadProviders();
    else { updateModelPickerUI(); MODEL_PICKER_TARGETS.forEach(t => renderModelPickerDropdown(t.dd)); }
    initRoleSelection();
    loadPolicy();
    showSettingsPage(page || currentSettingsPage || "general");
}

// ===== 执行策略（联网 / 安装 / 推送 / 评审跑测试）=====
const POLICY_CHECKBOXES = {
    allow_network: "polAllowNetwork",
    allow_package_install: "polAllowInstall",
    allow_git_push: "polAllowPush",
    allow_reviewer_tests: "polAllowReviewerTests",
};

function renderPolicy(data) {
    const policy = (data && data.policy) || {};
    const locked = (data && data.locked_by_env) || {};
    let anyLocked = false;
    Object.entries(POLICY_CHECKBOXES).forEach(([flag, id]) => {
        const el = document.getElementById(id);
        if (!el) return;
        el.checked = !!policy[flag];
        const isLocked = !!locked[flag];
        el.disabled = isLocked;
        if (isLocked) anyLocked = true;
    });
    const warn = document.getElementById("policyEnvWarn");
    if (warn) warn.style.display = anyLocked ? "block" : "none";
    const hint = document.getElementById("policyPathHint");
    if (hint && data && data.file) {
        hint.textContent = `配置保存在：${data.file}（当前沙箱网络：${data.sandbox_network || "-"}）`;
    }
}

async function loadPolicy() {
    try {
        const res = await fetch("/api/policy");
        if (!res.ok) return;
        renderPolicy(await res.json());
    } catch (e) { /* 策略接口不可用时静默——不影响其它设置项 */ }
}

async function setPolicyFlag(flag, value) {
    try {
        const res = await fetch("/api/policy", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ [flag]: value }),
        });
        if (!res.ok) throw new Error(await res.text());
        renderPolicy(await res.json());
    } catch (e) {
        alert("保存执行策略失败：" + e.message);
        loadPolicy();   // 回滚界面到服务端的真实状态
    }
}

function closeSettings() {
    document.getElementById("settingsModal").style.display = "none";
}

// ===== 项目约定（AGENTS.md）=====
let pcData = null;

async function loadProjectContext() {
    const box = document.getElementById("pcStatus");
    if (!box) return;
    try {
        const res = await fetch(`/api/project_context?workspace_id=${currentWorkspaceId || ""}`);
        pcData = await res.json();
    } catch (e) {
        box.innerHTML = `<div class="mcp-err">读取失败：${escapeHtml(e.message)}</div>`;
        return;
    }
    renderProjectContext();
}

function renderProjectContext() {
    const box = document.getElementById("pcStatus");
    const pre = document.getElementById("pcPreview");
    if (!box || !pcData) return;
    const srcs = pcData.sources || [];
    if (!srcs.length) {
        box.innerHTML = `<div class="set-hint">
                    当前工作区还没有 AGENTS.md。
                    点「新建 / 编辑」会在 <code>${escapeHtml(pcData.target_path || "")}</code>
                    建一个模板并在右侧面板打开。</div>`;
        pre.style.display = "none";
        return;
    }
    box.innerHTML = srcs.map(x => `
                <div class="pc-src">
                    <span class="pc-path">${escapeHtml(x.path)}</span>
                    <span class="pc-size">${x.chars} 字符${x.truncated ? "（已截断）" : ""}</span>
                </div>`).join("");
    pre.textContent = (pcData.preview || "") + (pcData.chars > 600 ? "\n…" : "");
    pre.style.display = "block";
}

/** 新建（写模板）或直接打开已有的 AGENTS.md。 */
async function editProjectContext() {
    if (!currentWorkspaceId) { alert("请先选择一个工作区"); return; }
    const target = pcData && pcData.target_path;
    const exists = pcData && (pcData.sources || []).some(
        x => target && x.path === target);
    if (!exists) {
        const tpl = [
            "# 项目约定",
            "",
            "<!-- 这里写的内容会在每次对话时注入给 Agent，请保持简洁。 -->",
            "",
            "## 这是什么项目",
            "",
            "## 技术栈与约定",
            "",
            "- ",
            "",
            "## 注意事项",
            "",
            "- ",
        ].join("\n");
        try {
            const res = await fetch("/api/project_context", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ workspace_id: currentWorkspaceId, content: tpl }),
            });
            const j = await res.json();
            if (!j.ok) { alert(j.error || "创建失败"); return; }
            await loadProjectContext();
        } catch (e) {
            alert("创建失败：" + e.message);
            return;
        }
    }
    closeSettings();
    openFile(pcData.target_path);   // 用右侧 CodeMirror 编辑
}

// ===== MCP 插件 =====
let mcpData = { servers: [], inject_enabled: true };
// 目录数据按需加载

async function loadMcpServers() {
    try {
        const res = await fetch("/api/mcp/servers");
        const j = await res.json();
        if (j.ok) { applyMcpData(j); }
    } catch (e) {
        console.warn("[MCP] 加载失败", e);
    }
}

function applyMcpData(j) {
    mcpData = { servers: j.servers || [], inject_enabled: j.inject_enabled !== false };
    const t = document.getElementById("mcpInjectToggle");
    if (t) t.checked = mcpData.inject_enabled;
    renderMcpServers();
}

const MCP_STATUS_LABEL = {
    ready: "🟢 已连接", connecting: "🟡 连接中", error: "🔴 连接失败",
    idle: "⚪ 未启用", stopped: "⚪ 已停止",
};

function renderMcpServers() {
    const box = document.getElementById("mcpServers");
    if (!box) return;
    if (!mcpData.servers.length) {
        box.innerHTML = `<div class="set-hint">还没有插件。点下面的「添加插件」。</div>`;
        return;
    }
    box.innerHTML = mcpData.servers.map(sv => {
        const st = MCP_STATUS_LABEL[sv.runtime_status] || sv.runtime_status;
        const tools = (sv.tools || []).length
            ? `<div class="mcp-tools">${sv.tools.map(t => `<code>${escapeHtml(t)}</code>`).join("")}</div>`
            : "";
        const err = sv.runtime_error
            ? `<div class="mcp-err">${escapeHtml(sv.runtime_error.slice(0, 200))}</div>` : "";
        const permLabel = { read: "只读", internal: "内部", mutate: "需确认", execute: "命令类" }[sv.permission] || sv.permission;
        return `
                <div class="mcp-row">
                    <div class="mcp-head">
                        <span class="mcp-name">${escapeHtml(sv.name)}</span>
                        <span class="mcp-status">${st}${sv.tool_count ? " · " + sv.tool_count + " 个工具" : ""}</span>
                    </div>
                    <div class="mcp-meta">
                        <span class="mcp-tag">${escapeHtml(permLabel)}</span>
                        <span class="mcp-tag">${escapeHtml(sv.inject)}</span>
                        ${sv.allow_network ? '<span class="mcp-tag net">🌐 已授权联网</span>' : '<span class="mcp-tag">🔒 不联网</span>'}
                        ${sv.sandbox ? '<span class="mcp-tag ok">📦 沙箱</span>' : '<span class="mcp-tag warn">⚠️ 跑在宿主机</span>'}
                        ${sv.resource_count ? `<span class="mcp-tag">${sv.resource_count} 资源</span>` : ""}
                        ${sv.prompt_count ? `<span class="mcp-tag">${sv.prompt_count} 提示词</span>` : ""}
                        ${sv.restart_count ? `<span class="mcp-tag warn">重连 ${sv.restart_count} 次</span>` : ""}
                    </div>
                    ${tools}${err}
                    <div class="mcp-row-btns">
                        <button class="fp-btn" onclick="toggleMcpServer('${escapeHtml(sv.name)}', ${!sv.enabled})">
                            ${sv.enabled ? "停用" : "启用"}</button>
                        <button class="fp-btn" onclick="reconnectMcpServer('${escapeHtml(sv.name)}')"
                            ${sv.enabled ? "" : "disabled"}>重连</button>
                        <button class="fp-btn" onclick="editMcpServer('${escapeHtml(sv.name)}')">编辑</button>
                        <button class="fp-btn danger" onclick="deleteMcpServer('${escapeHtml(sv.name)}')">删除</button>
                    </div>
                </div>`;
    }).join("");
}

async function mcpPost(url, body) {
    const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}),
    });
    const j = await res.json();
    if (!j.ok) { alert(j.error || j.message || "操作失败"); return null; }
    applyMcpData(j);
    return j;
}

function toggleMcpServer(name, enable) {
    if (enable && !confirm(
                `启用 MCP 插件「${name}」？\n\n它会作为一个程序运行在你的电脑上，` +
                `拥有和你同等的文件权限。只启用你信任的插件。`)) return;
    mcpPost(`/api/mcp/servers/${encodeURIComponent(name)}/enabled`, { enabled: enable })
        .then(j => { if (j && j.message) console.log("[MCP]", j.message); });
}

function reconnectMcpServer(name) {
    mcpPost(`/api/mcp/servers/${encodeURIComponent(name)}/reconnect`, {});
}

function deleteMcpServer(name) {
    if (!confirm(`删除插件「${name}」？（会断开连接）`)) return;
    fetch(`/api/mcp/servers/${encodeURIComponent(name)}`, { method: "DELETE" })
        .then(r => r.json())
        .then(j => { if (j.ok) applyMcpData(j); else alert(j.error || "删除失败"); });
}

function setMcpInject(enabled) {
    mcpPost("/api/mcp/inject", { enabled });
}

function showMcpForm(prefill) {
    const f = document.getElementById("mcpForm");
    f.style.display = "block";
    const v = prefill || {};
    document.getElementById("mcpName").value = v.name || "";
    document.getElementById("mcpName").readOnly = !!prefill;   // 改名会丢注册，禁止
    document.getElementById("mcpTransport").value = v.transport || "stdio";
    document.getElementById("mcpCommand").value = v.command || "";
    document.getElementById("mcpArgs").value = (v.args || []).join(" ");
    document.getElementById("mcpUrl").value = v.url || "";
    document.getElementById("mcpPermission").value = v.permission || "mutate";
    document.getElementById("mcpInject").value = v.inject || "auto";
    document.getElementById("mcpKeywords").value = (v.keywords || []).join(",");
    document.getElementById("mcpAllowNetwork").checked = !!v.allow_network;
    document.getElementById("mcpAutoReconnect").checked = v.auto_reconnect !== false;
    document.getElementById("mcpSandbox").checked = !!v.sandbox;
    document.getElementById("mcpSandboxImage").value = v.sandbox_image || "agent-mcp-sandbox";
    document.getElementById("mcpSandboxMounts").value = (v.sandbox_mounts || []).join(", ");
    onMcpTransportChange();
    onMcpSandboxChange();
}

/** stdio 与远程类型的字段互斥显示。 */
function onMcpTransportChange() {
    const t = document.getElementById("mcpTransport").value;
    const stdio = (t === "stdio");
    document.querySelectorAll("#mcpForm .mcp-stdio-only").forEach(e => e.style.display = stdio ? "" : "none");
    document.querySelectorAll("#mcpForm .mcp-remote-only").forEach(e => e.style.display = stdio ? "none" : "");
}

function onMcpSandboxChange() {
    const on = document.getElementById("mcpSandbox").checked;
    document.getElementById("mcpSandboxOpts").style.display = on ? "block" : "none";
}

function hideMcpForm() {
    document.getElementById("mcpForm").style.display = "none";
    document.getElementById("mcpCatalog").style.display = "none";
}

// ===== 内置插件目录 =====
let mcpCatalogData = [];

async function showMcpCatalog() {
    const box = document.getElementById("mcpCatalog");
    if (box.style.display !== "none") { box.style.display = "none"; return; }
    box.style.display = "block";
    box.innerHTML = `<div class="set-hint">加载中…</div>`;
    try {
        const res = await fetch("/api/mcp/catalog");
        const j = await res.json();
        mcpCatalogData = j.catalog || [];
    } catch (e) {
        box.innerHTML = `<div class="mcp-err">目录加载失败：${escapeHtml(e.message)}</div>`;
        return;
    }
    box.innerHTML = mcpCatalogData.map(it => {
        const disabled = it.runtime_available ? "" : "disabled";
        const tag = it.runtime_available
            ? ""
            : `<span class="mcp-tag">缺少 ${escapeHtml(it.runtime)}</span>`;
        const net = it.needs_network ? `<span class="mcp-tag net">🌐 需要联网</span>` : "";
        const box_ = it.recommend_sandbox ? `<span class="mcp-tag">📦 建议沙箱</span>` : "";
        return `
                <div class="cat-row">
                    <div class="cat-head">
                        <span class="mcp-name">${it.icon} ${escapeHtml(it.name)}</span>
                        <button class="fp-btn primary" ${disabled}
                            onclick="pickCatalog('${it.id}')">添加</button>
                    </div>
                    <div class="cat-desc">${escapeHtml(it.description)}</div>
                    <div class="mcp-meta">${tag}${net}${box_}
                        <span class="mcp-tag">${escapeHtml(it.permission)}</span>
                    </div>
                    ${it.notes ? `<div class="set-hint" style="margin-top:4px;">${escapeHtml(it.notes)}</div>` : ""}
                </div>`;
    }).join("");
}

function pickCatalog(id) {
    const it = mcpCatalogData.find(x => x.id === id);
    if (!it) return;
    if (!it.runtime_available) {
        alert(`这一项需要 ${it.runtime}（${it.runtime_hint}），本机没有装。`);
        return;
    }
    // 收集占位符
    let values = {};
    for (const ph of (it.placeholders || [])) {
        const v = prompt(`${it.name} · ${ph.label}`, ph.default || "");
        if (v === null) return;
        values[ph.key] = v;
    }
    fetch(`/api/mcp/catalog/${encodeURIComponent(id)}/config`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: id, values }),
    })
        .then(r => r.json())
        .then(j => {
            if (!j.ok) { alert(j.error || "生成配置失败"); return; }
            document.getElementById("mcpCatalog").style.display = "none";
            showMcpForm(j.config);
        })
        .catch(e => alert("生成配置失败：" + e.message));
}

function editMcpServer(name) {
    const sv = mcpData.servers.find(x => x.name === name);
    if (sv) showMcpForm(sv);
}

async function saveMcpServer() {
    const name = document.getElementById("mcpName").value.trim();
    const args = document.getElementById("mcpArgs").value.trim();
    const allowNet = document.getElementById("mcpAllowNetwork").checked;
    if (allowNet && !confirm(
                `允许「${name}」联网？\n\n这意味着它可以把数据发到外部服务器。`)) {
        document.getElementById("mcpAllowNetwork").checked = false;
        return;
    }
    const transport = document.getElementById("mcpTransport").value;
    const useSandbox = document.getElementById("mcpSandbox").checked;
    if (useSandbox && !confirm(
                `把「${name}」放进 Docker 沙箱运行？\n\n` +
                `它会只能看到你在「挂载目录」里列的路径，联网由上面的开关控制。`)) {
        document.getElementById("mcpSandbox").checked = false;
        return;
    }
    const body = {
        name,
        transport,
        command: document.getElementById("mcpCommand").value.trim(),
        // 参数按空格切分；带空格的路径请用引号
        args: args ? args.match(/(?:[^\s"]+|"[^"]*")+/g).map(a => a.replace(/^"|"$/g, "")) : [],
        url: document.getElementById("mcpUrl").value.trim(),
        permission: document.getElementById("mcpPermission").value,
        inject: document.getElementById("mcpInject").value,
        keywords: document.getElementById("mcpKeywords").value.split(",").map(k => k.trim()).filter(Boolean),
        allow_network: allowNet,
        auto_reconnect: document.getElementById("mcpAutoReconnect").checked,
        sandbox: useSandbox,
        sandbox_image: document.getElementById("mcpSandboxImage").value.trim() || "agent-mcp-sandbox",
        sandbox_mounts: document.getElementById("mcpSandboxMounts").value
            .split(",").map(m => m.trim()).filter(Boolean),
        enabled: (mcpData.servers.find(x => x.name === name) || {}).enabled || false,
    };
    const j = await mcpPost("/api/mcp/servers", body);
    if (j) hideMcpForm();
}

// ============ 模型提供商管理 ============
