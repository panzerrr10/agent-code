function showWriteConfirm(ctx, path, oldContent, newContent) {
    ctx.pendingWriteContent = newContent;
    const existing = ctx.bubble.parentElement.querySelector(".confirm-actions");
    if (existing) existing.remove();

    // 生成简单的 diff
    const diffHtml = generateDiff(oldContent, newContent);

    const div = document.createElement("div");
    div.className = "confirm-actions";
    div.style.flexDirection = "column";
    div.style.alignItems = "stretch";
    div.innerHTML = `
                <div style="display:flex;align-items:center;gap:8px;">
                    <span class="confirm-text" style="flex:1;">✍️ 确认写入 <b>${escapeHtml(path)}</b>？</span>
                    <button class="confirm-btn yes" title="确认写入">✓</button>
                    <button class="confirm-btn no" title="取消">✗</button>
                </div>
                <div class="diff-view">${diffHtml}</div>
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
        cancelMsg.textContent = "❌ 已取消写入";
        ctx.bubble.parentElement.appendChild(cancelMsg);
    });

    ctx.bubble.parentElement.appendChild(div);
    chat.scrollTop = chat.scrollHeight;
}

function generateDiff(oldText, newText) {
    const oldLines = oldText.split("\n");
    const newLines = newText.split("\n");

    // 简易 LCS diff：找出最长公共子序列，标记增删
    const m = oldLines.length;
    const n = newLines.length;

    // DP 表
    const dp = Array(m + 1).fill(null).map(() => Array(n + 1).fill(0));
    for (let i = 1; i <= m; i++) {
        for (let j = 1; j <= n; j++) {
            if (oldLines[i - 1] === newLines[j - 1]) {
                dp[i][j] = dp[i - 1][j - 1] + 1;
            } else {
                dp[i][j] = Math.max(dp[i - 1][j], dp[i][j - 1]);
            }
        }
    }

    // 回溯生成 diff
    const result = [];
    let i = m, j = n;
    while (i > 0 || j > 0) {
        if (i > 0 && j > 0 && oldLines[i - 1] === newLines[j - 1]) {
            result.unshift({ type: "same", oldLine: i, newLine: j, text: oldLines[i - 1] });
            i--;
            j--;
        } else if (j > 0 && (i === 0 || dp[i][j - 1] >= dp[i - 1][j])) {
            result.unshift({ type: "add", newLine: j, text: newLines[j - 1] });
            j--;
        } else if (i > 0) {
            result.unshift({ type: "del", oldLine: i, text: oldLines[i - 1] });
            i--;
        }
    }

    // 转成 HTML
    let html = "";
    for (const item of result) {
        if (item.type === "same") {
            html += `<span class="diff-line diff-unchanged">` +
                        `<span class="diff-lineno">${item.oldLine}</span>` +
                        `<span class="diff-lineno">${item.newLine}</span>` +
                        `  ${escapeHtml(item.text)}</span>`;
        } else if (item.type === "del") {
            html += `<span class="diff-line diff-del">` +
                        `<span class="diff-lineno">${item.oldLine}</span>` +
                        `<span class="diff-lineno"></span>` +
                        `- ${escapeHtml(item.text)}</span>`;
        } else if (item.type === "add") {
            html += `<span class="diff-line diff-add">` +
                        `<span class="diff-lineno"></span>` +
                        `<span class="diff-lineno">${item.newLine}</span>` +
                        `+ ${escapeHtml(item.text)}</span>`;
        }
    }

    return html || '<span class="diff-unchanged">（无变化）</span>';
}

// ============ 确认后续跑 Agent ============
// 用户点 ✓ 后调用：带 resume:true 重新请求 /run，复用后端 pending state，
// 执行被确认的工具，并从原 step 继续跑完剩余步骤（含链式确认）。
// ============ 统一的 SSE 事件处理 ============
//
// **为什么要有这一段**：runTask（首次运行）和 resumeRun（续跑）原本各自
// 复制了一份完全一样的事件分支。复制带来的代价不是「代码多」，而是
// **只往其中一份加处理器，另一份就静默漏掉这个事件**。
//
// 真实事故：上下文压缩指示、项目约定提示、权限拒绝卡片、轮次事件
// 全都只加在了 resumeRun 那一份 —— 主路径下这些 UI 一个都不显示，
// 而且单元测试和 CDP 都没发现（CDP 当时用合成事件直接调渲染函数，
// 绕过了真实的事件分发）。
//
// 现在只有一份：任何事件处理都改这里，两条路径同时生效。

/**
 * 处理单条 SSE 事件。
 *
 * @param {object} run 本次运行的上下文（由调用方构造，见 makeRunContext）
 * @param {object} ev  后端发来的事件
 */
