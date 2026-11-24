let _treeData = null;

function toggleProjectTree() {
    const panel = document.getElementById("projectTreePanel");
    const btn = document.getElementById("ptToggleBtn");
    const collapsed = panel.classList.toggle("collapsed");
    btn.textContent = collapsed ? "▶" : "◀";
    localStorage.setItem("projectTreeCollapsed", collapsed ? "1" : "0");
}

function restoreProjectTreeState() {
    const collapsed = localStorage.getItem("projectTreeCollapsed") === "1";
    if (collapsed) {
        const panel = document.getElementById("projectTreePanel");
        const btn = document.getElementById("ptToggleBtn");
        if (panel && btn) {
            panel.classList.add("collapsed");
            btn.textContent = "▶";
        }
    }
}

async function loadProjectTree() {
    // 项目树展示的就是「当前工作区」的目录
    const ws = currentWorkspace();
    const root = ws ? ws.path : "";
    if (!root) {
        document.getElementById("projectTree").innerHTML = '<div style="padding: 8px 12px; color: #9ca3af; font-size: 12px;">（未选择工作区）</div>';
        return;
    }
    if (!ws.exists) {
        document.getElementById("projectTree").innerHTML = `<div style="padding: 8px 12px; color: #ef4444; font-size: 12px;">⚠️ 工作区目录不存在：${escapeHtml(root)}</div>`;
        return;
    }
    try {
        const res = await fetch("/api/project_tree?path=" + encodeURIComponent(root) + "&depth=3");
        const data = await res.json();
        if (data.error) {
            document.getElementById("projectTree").innerHTML = `<div style="padding: 8px 12px; color: #ef4444; font-size: 12px;">❌ ${data.error}</div>`;
            return;
        }
        _treeData = data.tree;
        renderTree(data.tree);
    } catch (e) {
        document.getElementById("projectTree").innerHTML = `<div style="padding: 8px 12px; color: #ef4444; font-size: 12px;">❌ ${e.message}</div>`;
    }
}

function renderTree(node) {
    const container = document.getElementById("projectTree");
    container.innerHTML = "";
    if (!node || !node.children) return;
    node.children.forEach(child => {
        container.appendChild(renderTreeNode(child, 0));
    });
}

function renderTreeNode(node, level) {
    const div = document.createElement("div");
    const item = document.createElement("div");
    item.className = "tree-item " + node.type;
    item.style.paddingLeft = (12 + level * 12) + "px";

    if (node.type === "dir") {
        item.innerHTML = `<span class="icon">📁</span>`;
        const nameSpan = document.createElement("span");
        nameSpan.textContent = node.name;
        item.appendChild(nameSpan);
        item.onclick = () => {
            // 展开/折叠
            const children = div.querySelector(".tree-children");
            if (children) {
                children.style.display = children.style.display === "none" ? "block" : "none";
            }
        };
        div.appendChild(item);

        const children = document.createElement("div");
        children.className = "tree-children";
        children.style.display = "none";  // 默认折叠
        if (node.children) {
            node.children.forEach(c => children.appendChild(renderTreeNode(c, level + 1)));
        }
        div.appendChild(children);
    } else {
        item.innerHTML = `<span class="icon">📄</span>`;
        const nameSpan2 = document.createElement("span");
        nameSpan2.textContent = node.name;
        item.appendChild(nameSpan2);
        item.onclick = () => {
            // 点击文件 → 在右侧面板打开。
            // 按用户要求不再往对话输入框塞文本（那样看个文件要烧一整轮 Agent）。
            // 想交给 Agent 处理时，面板底部有「🤖 让 Agent 处理」按钮。
            document.querySelectorAll("#projectTree .tree-item.active")
                .forEach(el => el.classList.remove("active"));
            item.classList.add("active");
            openFile(node.path);
        };
        div.appendChild(item);
    }
    return div;
}

// ============ 右侧文件面板（CodeMirror 6 · 多标签）============
