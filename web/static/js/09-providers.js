// 提供商**全部由用户在这里定义**，后端不再有预设。
// 硬约束：完整密钥只进不出 —— 界面永远只显示掩码，保存时留空表示「不修改」。

/** 提供商列表：卡片式，每张卡能改、能删、能测、能设为默认/嵌入。 */
function renderProviderKeys() {
    const box = document.getElementById("providerKeys");
    if (!box) return;

    if (!providersData.length) {
        box.innerHTML = `
                    <div class="prov-empty">
                        还没有配置任何模型提供商。<br>
                        点右上角「＋ 添加模型提供商」，填入 Base URL、API Key 和模型名称即可开始使用。
                    </div>`;
        renderProviderSelects();
        return;
    }

    box.innerHTML = providersData.map(p => {
        const isDefault = p.id === providersDefault;
        const isRag = p.id === providersRag;
        const models = (p.models || []).map(m =>
                    `<span class="prov-model">${escapeHtml(m)}</span>`).join("");
        return `
                <div class="prov-card" data-pid="${p.id}">
                    <div class="prov-top">
                        <span class="prov-icon">${p.icon || "🧠"}</span>
                        <span class="prov-name">${escapeHtml(p.name)}</span>
                        ${isDefault ? '<span class="prov-tag def">默认</span>' : ""}
                        ${isRag ? '<span class="prov-tag rag">嵌入</span>' : ""}
                        <span class="prov-state ${p.has_key ? "ok" : "miss"}">
                            ${p.has_key ? "🔒 " + escapeHtml(p.key_hint || "已配置") : "⚠️ 未配置密钥"}
                        </span>
                        <span class="prov-ops">
                            <button class="fp-btn" onclick="testProvider('${p.id}', this)"
                                    title="真发一次最小请求验证">测试</button>
                            <button class="fp-btn" onclick="openProviderForm('${p.id}')">编辑</button>
                            <button class="fp-btn danger" onclick="deleteProviderCfg('${p.id}')">删除</button>
                        </span>
                    </div>
                    <div class="prov-url">${escapeHtml(p.base_url || "（未填 Base URL）")}</div>
                    <div class="prov-models">${models || '<span class="prov-model empty">（未填模型）</span>'}</div>
                </div>`;
    }).join("");
    renderProviderSelects();
}

/** 默认 / 嵌入两个下拉。 */
function renderProviderSelects() {
    const mk = (sel, cur, allowEmpty) => {
        if (!sel) return;
        const opts = providersData.map(p =>
                    `<option value="${p.id}" ${p.id === cur ? "selected" : ""}>${escapeHtml(p.name)}</option>`);
        sel.innerHTML = (allowEmpty ? `<option value="">（不指定）</option>` : "") + opts.join("");
        if (allowEmpty) sel.value = cur || "";
    };
    mk(document.getElementById("provDefaultSel"), providersDefault, false);
    mk(document.getElementById("provRagSel"), providersRag, true);
    const sec = document.getElementById("provDefaultsSec");
    if (sec) sec.style.display = providersData.length ? "" : "none";
}

// ---- 添加 / 编辑表单 ----

let provEditingId = null;

function openProviderForm(pid) {
    provEditingId = pid || null;
    const p = pid ? providersData.find(x => x.id === pid) : null;
    document.getElementById("provFormTitle").textContent =
        p ? `编辑「${p.name}」` : "添加模型提供商";
    document.getElementById("pfName").value = p ? p.name : "";
    document.getElementById("pfUrl").value = p ? (p.base_url || "") : "";
    document.getElementById("pfKey").value = "";     // 永不回显密钥
    document.getElementById("pfKeyNote").textContent = p && p.has_key
        ? `已配置（${p.key_hint}）—— 留空则不修改` : "";
    document.getElementById("pfModels").value = p ? (p.models || []).join("\n") : "";
    document.getElementById("pfEmbedding").checked = !!(p && p.supports_embedding);
    document.getElementById("pfResult").textContent = "";
    document.getElementById("provFormMask").style.display = "flex";
    setTimeout(() => document.getElementById("pfName").focus(), 50);
}

function closeProviderForm() {
    document.getElementById("provFormMask").style.display = "none";
    provEditingId = null;
}

function _providerFormPayload() {
    return {
        id: provEditingId || undefined,
        name: document.getElementById("pfName").value.trim(),
        base_url: document.getElementById("pfUrl").value.trim(),
        api_key: document.getElementById("pfKey").value.trim(),   // 空 = 不修改
        models: document.getElementById("pfModels").value,
        supports_embedding: document.getElementById("pfEmbedding").checked,
    };
}

function _applyProviderResponse(j) {
    providersData = j.providers || [];
    providersDefault = j.default || "";
    providersRag = j.rag_provider || "";
    renderProviderKeys();
    MODEL_PICKER_TARGETS.forEach(t => renderModelPickerDropdown(t.dd));
    if (multiAgentMode) initRoleSelection();
}

async function submitProviderForm(btn) {
    const payload = _providerFormPayload();
    if (!payload.name) { alert("请填写名称"); return; }
    if (!payload.base_url) { alert("请填写 Base URL"); return; }
    // 不强制 API Key —— 本地模型（Ollama / LM Studio）不需要
    if (!payload.models.trim()) { alert("请填写至少一个模型名称"); return; }

    btn.disabled = true;
    try {
        const res = await fetch("/api/providers", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const j = await res.json();
        if (!j.ok) { alert(j.error || "保存失败"); return; }
        _applyProviderResponse(j);
        closeProviderForm();
    } catch (e) {
        alert("保存失败：" + e.message);
    } finally {
        btn.disabled = false;
    }
}

/** 测试连接：真的发一次请求 —— 只校验格式发现不了「URL 少了 /v1」。 */
async function testProviderForm(btn) {
    const payload = _providerFormPayload();
    const out = document.getElementById("pfResult");
    if (!payload.base_url) { out.textContent = "请先填 Base URL"; return; }
    if (!payload.api_key && !provEditingId) { out.textContent = "请先填 API Key"; return; }
    const first = String(payload.models || "").split(/[,\n]/).map(x => x.trim())
        .filter(Boolean)[0] || "";
    if (!first) { out.textContent = "请先填模型名称"; return; }

    btn.disabled = true;
    out.className = "pf-result";
    out.textContent = "正在测试…";
    try {
        const res = await fetch("/api/providers/test", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ...payload, model: first }),
        });
        const j = await res.json();
        out.className = "pf-result " + (j.ok ? "ok" : "err");
        out.textContent = j.ok ? ("✅ " + j.message) : ("❌ " + j.error);
    } catch (e) {
        out.className = "pf-result err";
        out.textContent = "❌ 测试失败：" + e.message;
    } finally {
        btn.disabled = false;
    }
}

async function testProvider(pid, btn) {
    btn.disabled = true;
    const old = btn.textContent;
    btn.textContent = "测试中…";
    try {
        const res = await fetch("/api/providers/test", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ id: pid }),
        });
        const j = await res.json();
        alert(j.ok ? `✅ ${j.message}` : `❌ ${j.error}`);
    } catch (e) {
        alert("测试失败：" + e.message);
    } finally {
        btn.disabled = false;
        btn.textContent = old;
    }
}

async function deleteProviderCfg(pid) {
    const p = providersData.find(x => x.id === pid);
    if (!confirm(`删除提供商「${p ? p.name : pid}」？\n它的密钥也会一并从服务端删除。`)) return;
    try {
        const res = await fetch(`/api/providers/${encodeURIComponent(pid)}`, { method: "DELETE" });
        const j = await res.json();
        if (!j.ok) { alert(j.error || "删除失败"); return; }
        _applyProviderResponse(j);
    } catch (e) {
        alert("删除失败：" + e.message);
    }
}

async function setDefaultProvider(pid) {
    if (!pid) return;
    const res = await fetch("/api/providers/default", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: pid }),
    });
    const j = await res.json();
    if (!j.ok) { alert(j.error || "设置失败"); return; }
    providersDefault = j.default;
    renderProviderKeys();
    MODEL_PICKER_TARGETS.forEach(t => renderModelPickerDropdown(t.dd));
}

async function setRagProvider(pid) {
    const res = await fetch("/api/providers/rag", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: pid }),
    });
    const j = await res.json();
    if (!j.ok) { alert(j.error || "设置失败"); return; }
    providersRag = j.rag_provider || "";
    renderProviderKeys();
}

async function loadPermission() {
    if (!currentSessionId) return;
    try {
        const res = await fetch(`/sessions/${currentSessionId}/permission`);
        const j = await res.json();
        if (j.ok) { currentPermission = j.level; renderPermission(); }
    } catch (e) {
        console.warn("[PERM] 读取失败", e);
    }
}

function clearAttachments() {
    pendingAttachments = [];
    renderAttachStrip();
}

/** 下载文件到浏览器默认下载目录（本机实测为 /home/pz/下载）。 */
function downloadPath(path, name) {
    const url = `/api/download?path=${encodeURIComponent(path)}`
        + `&name=${encodeURIComponent(name || "")}`;
    // 走 fetch + Blob 才能带上 X-Auth-Token（导航式下载带不了自定义头）
    fetch(url)
        .then(r => { if (!r.ok) throw new Error("HTTP " + r.status); return r.blob(); })
        .then(blob => {
            const a = document.createElement("a");
            a.href = URL.createObjectURL(blob);
            a.download = name || String(path).split("/").pop();
            document.body.appendChild(a);
            a.click();
            a.remove();
            setTimeout(() => URL.revokeObjectURL(a.href), 5000);
        })
        .catch(e => alert("下载失败：" + e.message));
}

function clearImages() {
    pendingImages = [];
    renderImagePreview();
}

function addImageFiles(files) {
    const list = Array.from(files || []);
    for (const file of list) {
        if (!file.type || !file.type.startsWith("image/")) continue;
        if (file.size > MAX_IMAGE_BYTES) {
            alert(`图片过大：${file.name || "image"}（${(file.size / 1024 / 1024).toFixed(1)}MB），上限 10MB`);
            continue;
        }
        compressImage(file, 1024, 0.85)
            .then(dataUrl => {
                pendingImages.push({ dataUrl, name: file.name || "image" });
                renderImagePreview();
            })
            .catch(err => {
                console.warn("[IMG] 压缩失败，跳过：", err);
                alert(`图片处理失败：${file.name || "image"}`);
            });
    }
}

function closeLightbox(e) {
    // 点击图片本身不关闭，只有点击遮罩层才关闭
    if (e && e.target && e.target.id === "imgLightboxSrc") return;
    const box = document.getElementById("imgLightbox");
    if (box) box.classList.remove("show");
}

/** 回形针统一入口：按类型分流。 */
async function handleAttachFiles(files) {
    const list = Array.from(files || []);
    const images = list.filter(f => f.type && f.type.startsWith("image/"));
    const docs = list.filter(f => !(f.type && f.type.startsWith("image/")));
    if (images.length) addImageFiles(images);
    for (const f of docs) await uploadAttachment(f);
    // 允许连续选择同一个文件
    const input = document.getElementById("attachInput");
    if (input) input.value = "";
}

function openLightbox(src) {
    const box = document.getElementById("imgLightbox");
    const img = document.getElementById("imgLightboxSrc");
    if (!box || !img) return;
    img.src = src;
    box.classList.add("show");
}

function removeAttachment(index) {
    pendingAttachments.splice(index, 1);
    renderAttachStrip();
}

function removeImage(index) {
    pendingImages.splice(index, 1);
    renderImagePreview();
}

function renderAttachStrip() {
    const box = document.getElementById("attachStrip");
    if (!box) return;
    if (!pendingAttachments.length) {
        box.style.display = "none";
        box.innerHTML = "";
        return;
    }
    box.style.display = "flex";
    box.innerHTML = pendingAttachments.map((a, i) => `
                <span class="attach-chip" title="${escapeHtml(a.path)}">
                    <span class="attach-name">📄 ${escapeHtml(a.name)}</span>
                    <span class="attach-size">${a.size < 1024 ? a.size + " B" : (a.size / 1024).toFixed(1) + " KB"}</span>
                    <button type="button" onclick="openFile('${escapeHtml(a.path).replace(/'/g, "\\'")}')"
                        title="在右侧面板打开">打开</button>
                    <button type="button" onclick="downloadPath('${escapeHtml(a.path).replace(/'/g, "\\'")}', '${escapeHtml(a.name).replace(/'/g, "\\'")}')"
                        title="下载（存到系统「下载」目录）">⬇</button>
                    <button type="button" class="attach-del" onclick="removeAttachment(${i})"
                        title="移除">✕</button>
                </span>`).join("");
}

function renderImagePreview() {
    const box = document.getElementById("imagePreview");
    if (!box) return;
    if (!pendingImages.length) {
        box.style.display = "none";
        box.innerHTML = "";
        return;
    }
    box.style.display = "flex";
    box.innerHTML = pendingImages.map((img, i) => `
                <div class="img-thumb" title="${escapeHtml(img.name)}">
                    <img src="${img.dataUrl}" alt="${escapeHtml(img.name)}" onclick="openLightbox('${img.dataUrl}')">
                    <button type="button" class="img-thumb-del" onclick="removeImage(${i})" title="移除">×</button>
                </div>
    `).join("");
}

function renderPermission() {
    document.querySelectorAll("#permSwitch .perm-btn").forEach(b => {
        const on = Number(b.dataset.level) === currentPermission;
        b.classList.toggle("active", on);
    });
    const box = document.getElementById("permSwitch");
    if (box) box.dataset.level = String(currentPermission);
}

async function setPermission(level) {
    if (!currentSessionId) { alert("请先选择一个会话"); return; }
    const prev = currentPermission;
    currentPermission = level;
    renderPermission();   // 先乐观更新，失败再回滚
    try {
        const res = await fetch(`/sessions/${currentSessionId}/permission`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ level }),
        });
        const j = await res.json();
        if (!j.ok) {
            currentPermission = prev;
            renderPermission();
            alert(j.error || "设置权限等级失败");
            return;
        }
        currentPermission = j.level;
        renderPermission();
    } catch (e) {
        currentPermission = prev;
        renderPermission();
        alert("设置权限等级失败：" + e.message);
    }
}

async function uploadAttachment(file) {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("session_id", String(currentSessionId || ""));
    try {
        const res = await fetch("/api/attach", { method: "POST", body: fd });
        const j = await res.json();
        if (!j.ok) { alert("附件上传失败：" + (j.error || "未知错误")); return; }
        pendingAttachments.push({
            name: j.name, path: j.path, size: j.size, chars: j.chars,
        });
        renderAttachStrip();
    } catch (e) {
        alert("附件上传失败：" + e.message);
    }
}

async function loadProviders() {
    try {
        const res = await fetch("/api/providers");
        const data = await res.json();
        providersData = data.providers || [];
        providersDefault = data.default || "";
        providersRag = data.rag_provider || "";

        // 一个提供商都没配：不要卡在「加载中」，直接引导用户去添加
        if (!providersData.length) {
            currentProviderId = "";
            currentModelName = "";
            MODEL_PICKER_TARGETS.forEach(t => {
                const el = document.getElementById(t.label);
                if (el) el.textContent = "未配置模型";
                const ic = document.getElementById(t.icon);
                if (ic) ic.textContent = "⚠️";
            });
            renderProviderKeys();
            return;
        }

        // 从 localStorage 恢复上次选择，或使用默认
        const savedPid = localStorage.getItem("providerId");
        const savedModel = localStorage.getItem("modelName");
        const defaultPid = providersDefault || (providersData[0] && providersData[0].id);

        if (savedPid && providersData.find(p => p.id === savedPid)) {
            currentProviderId = savedPid;
            currentModelName = savedModel || providersData.find(p => p.id === savedPid).default_model;
        } else {
            currentProviderId = defaultPid;
            const p = providersData.find(x => x.id === defaultPid);
            currentModelName = p ? p.default_model : "";
        }

        updateModelPickerUI();
        MODEL_PICKER_TARGETS.forEach(t => renderModelPickerDropdown(t.dd));
        renderProviderKeys();       // 设置面板里的密钥区
        if (multiAgentMode) initRoleSelection();   // 批次 4：provider 列表就绪后同步三角色选择器
    } catch (e) {
        console.error("[ModelPicker] 加载 provider 失败", e);
    }
}

// 模型选择有两处入口（输入框底部 + 设置面板），共用同一份状态与渲染，
// 这样在任一处切换，另一处立刻同步。
const MODEL_PICKER_TARGETS = [
    { label: "modelPickerLabel", icon: "modelPickerIcon", dd: "modelPickerDropdown" },
    { label: "settingsModelPickerLabel", icon: "settingsModelPickerIcon", dd: "settingsModelPickerDropdown" },
];

function updateModelPickerUI() {
    if (!providersData.length) return;      // 空列表由 loadProviders 统一提示
    const p = providersData.find(x => x.id === currentProviderId);
    if (!p) return;
    MODEL_PICKER_TARGETS.forEach(t => {
        const iconEl = document.getElementById(t.icon);
        const labelEl = document.getElementById(t.label);
        if (iconEl) iconEl.textContent = p.icon;
        if (labelEl) {
            // 空间不够时 `DeepSeek · deepseek-chat` 会被截成
            // 「DeepSeek · dee...」，认不出是哪个模型。
            // 窄的时候只留模型名（用户关心的是模型，不是厂商），
            // 全名放 title，鼠标悬停能看到。
            labelEl.textContent = currentModelName || p.name;
            const full = `${p.name} · ${currentModelName}`;
            labelEl.title = full;
            if (iconEl) iconEl.title = full;
        }
    });
}

/**
 * 下拉方向自适应：默认向下弹，下方空间不够就翻到上方。
 *
 * 之所以改成统一计算：之前是「每个下拉各自写死方向」，结果修好了单 Agent 的
 * 模型选择框、却漏了多 Agent 的三个角色选择器（它们用的是另一个 class
 * .role-picker-dropdown）。改成按可用空间算，以后把下拉挪到哪儿都不会错。
 */
function positionDropdown(dd, anchorEl) {
    const anchor = anchorEl || (dd && dd.parentElement);
    if (!dd || !anchor) return;
    dd.classList.remove("drop-up");        // 先按"向下"量高度
    const rect = anchor.getBoundingClientRect();
    const roomBelow = window.innerHeight - rect.bottom;
    const need = Math.min(dd.scrollHeight || 260, 340) + 14;
    if (roomBelow < need) dd.classList.add("drop-up");
}

function toggleModelPicker(event, dropdownId) {
    if (event) event.stopPropagation();
    const targetId = dropdownId || "modelPickerDropdown";
    // 先收起其它下拉，避免两个同时展开
    MODEL_PICKER_TARGETS.forEach(t => {
        if (t.dd !== targetId) {
            const other = document.getElementById(t.dd);
            if (other) other.classList.remove("show");
        }
    });
    const dd = document.getElementById(targetId);
    if (!dd) return;
    dd.classList.toggle("show");
    if (dd.classList.contains("show")) {
        const btn = dd.parentElement.querySelector("button");
        positionDropdown(dd, btn || dd.parentElement);
    }
}

function renderModelPickerDropdown(targetId) {
    const dd = document.getElementById(targetId || "modelPickerDropdown");
    if (!dd) return;
    dd.innerHTML = providersData.map(p => {
        const lockTag = p.has_key ? "" : '<span class="locked">🔒 未配置</span>';
        const items = p.models.map(m => {
            const isActive = (currentProviderId === p.id && currentModelName === m);
            const isDefault = (m === p.default_model);
            const disabledClass = p.has_key ? "" : "disabled";
            const onclick = p.has_key ? `selectModel('${p.id}', '${m}')` : "";
            return `
                        <div class="model-item ${isActive ? 'active' : ''} ${disabledClass}"
                             onclick="${onclick}">
                            <span>${m}${isDefault ? '<span class="default-tag">默认</span>' : ''}</span>
                        </div>
            `;
        }).join("");
        return `
                    <div class="provider-group">
                        <div class="provider-header">${p.icon} ${p.name} ${lockTag}</div>
                        ${items}
                    </div>
        `;
    }).join("");
}

function selectModel(providerId, model) {
    currentProviderId = providerId;
    currentModelName = model;
    updateModelPickerUI();
    MODEL_PICKER_TARGETS.forEach(t => {
        renderModelPickerDropdown(t.dd);
        const dd = document.getElementById(t.dd);
        if (dd) dd.classList.remove("show");
    });
    localStorage.setItem("providerId", providerId);
    localStorage.setItem("modelName", model);
}

// 点击外部关闭下拉
document.addEventListener("click", e => {
    ["modelPicker", "settingsModelPicker"].forEach(pid => {
        const picker = document.getElementById(pid);
        if (picker && !picker.contains(e.target)) {
            const dd = picker.querySelector(".model-picker-dropdown");
            if (dd) dd.classList.remove("show");
        }
    });
});

// 页面加载时拉取 provider 列表
if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", loadProviders);
} else {
    loadProviders();
}

// ===== 多 Agent 三角色选择器（批次 4）=====
const roleSelection = {
    planner: { providerId: "", model: "" },
    executor: { providerId: "", model: "" },
    reviewer: { providerId: "", model: "" },
};

function initRoleSelection() {
    if (!providersData.length) return;   // provider 列表未就绪，等 loadProviders 完成后重调
    for (const role of ["planner", "executor", "reviewer"]) {
        const savedPid = localStorage.getItem(`role_${role}_providerId`);
        const savedModel = localStorage.getItem(`role_${role}_model`);
        const savedProvider = savedPid ? providersData.find(p => p.id === savedPid) : null;
        if (savedProvider) {
            roleSelection[role].providerId = savedPid;
            roleSelection[role].model = savedModel || savedProvider.default_model;
        } else {
            // 默认继承单 Agent 选择器
            roleSelection[role].providerId = currentProviderId;
            roleSelection[role].model = currentModelName;
        }
        updateRolePickerUI(role);
    }
}

function updateRolePickerUI(role) {
    const sel = roleSelection[role];
    const p = providersData.find(x => x.id === sel.providerId);
    if (!p) return;
    const iconEl = document.getElementById(`${role}PickerIcon`);
    const labelEl = document.getElementById(`${role}PickerLabel`);
    if (iconEl) iconEl.textContent = p.icon;
    if (labelEl) labelEl.textContent = `${p.name} · ${sel.model}`;
}

function toggleRolePicker(role, event) {
    if (event) event.stopPropagation();
    // 关闭所有其他下拉
    document.querySelectorAll(".role-picker-dropdown").forEach(dd => dd.classList.remove("show"));
    const dd = document.getElementById(`${role}PickerDropdown`);
    if (dd) {
        dd.classList.toggle("show");
        if (dd.classList.contains("show")) {
            renderRolePickerDropdown(role);
            positionDropdown(dd, document.getElementById(`${role}PickerBtn`));
        }
    }
}

function renderRolePickerDropdown(role) {
    const dd = document.getElementById(`${role}PickerDropdown`);
    if (!dd) return;
    const sel = roleSelection[role];
    dd.innerHTML = providersData.map(p => {
        const lockTag = p.has_key ? "" : '<span class="locked">🔒 未配置</span>';
        const items = (p.models || []).map(m => {
            const isActive = (sel.providerId === p.id && sel.model === m);
            const isDefault = (m === p.default_model);
            const disabledClass = p.has_key ? "" : "disabled";
            const onclick = p.has_key ? `selectRoleModel('${role}', '${p.id}', '${m}')` : "";
            return `
                        <div class="model-item ${isActive ? 'active' : ''} ${disabledClass}"
                             onclick="${onclick}" tabindex="${p.has_key ? 0 : -1}" role="option"
                             aria-selected="${isActive}"
                             onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();this.click();}">
                            <span>${m}${isDefault ? '<span class="default-tag">默认</span>' : ''}</span>
                        </div>
            `;
        }).join("");
        return `
                    <div class="provider-group">
                        <div class="provider-header">${p.icon} ${p.name} ${lockTag}</div>
                        ${items}
                    </div>
        `;
    }).join("");
}

function selectRoleModel(role, providerId, model) {
    roleSelection[role].providerId = providerId;
    roleSelection[role].model = model;
    updateRolePickerUI(role);
    const dd = document.getElementById(`${role}PickerDropdown`);
    if (dd) dd.classList.remove("show");
    localStorage.setItem(`role_${role}_providerId`, providerId);
    localStorage.setItem(`role_${role}_model`, model);
}

// 点击外部关闭所有角色下拉
document.addEventListener("click", e => {
    let clickedInside = false;
    document.querySelectorAll(".role-picker").forEach(rp => { if (rp.contains(e.target)) clickedInside = true; });
    if (!clickedInside) {
        document.querySelectorAll(".role-picker-dropdown").forEach(dd => dd.classList.remove("show"));
    }
});

// ============ 执行任务 ============
