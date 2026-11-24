function handleSseEvent(run, ev) {
    const st = sessionStates[run.sessionId];
    if (st && !st.execHistory) st.execHistory = [];

    // ---- 第一步：先写内存。不论用户当前停在哪个会话，都要留痕 ----
    if (ev.type === "text") {
        run.fullText += ev.content;
        if (st) st.fullText = run.fullText;
    } else if (ev.type === "plan_update") {
        if (st) st.plan = ev.plan;
    } else if (ev.type === "tool_call" || ev.type === "tool_result"
               || ev.type === "phase" || ev.type === "plan_from_planner") {
        if (st) st.execHistory.push(ev);
    } else if (ev.type === "token_usage") {
        // 先记内存：用户可能正停在别的会话，用量仍要算到本会话头上。
        // 一次 run 只会发一个 token_usage 事件（正常收尾或超步数二选一），
        // 所以这里直接累加，不会重复计数。
        addUsage(run.sessionId, ev);
        if (run.isActive()) renderSessionStats(run.sessionId);
    }

    // ---- 第二步：用户不在本会话 → 只留内存，不碰 DOM ----
    if (!run.isActive()) {
        if (ev.type === "confirm_write" || ev.type === "confirm_delete"
            || ev.type === "confirm_command") {
            if (st) st.pendingConfirm = ev;
        }
        return;
    }

    // ---- 第三步：在本会话 → 更新界面 ----
    const ctx = run.ctx;
    if (ev.type === "text") {
        // 增量流式：每段都重解析全文太浪费，交给 rAF 合并到每帧一次
        scheduleTextRender(run);

    } else if (ev.type === "token_usage") {
        // 缓存命中率：固定前缀（系统提示 + 工具 schema + 代码库索引）
        // 每步都重发，命中率高说明这部分几乎免费；命中率低说明前缀
        // 被什么东西改动了（压缩改写、工具集变化…）。没有数据显示时
        // 明确写「未上报」，不要显示成 0% —— 那是两回事。
        let cacheTxt = " · 缓存 未上报";
        if (ev.cache_hit_tokens || ev.cache_miss_tokens) {
            const rate = typeof ev.cache_hit_rate === "number"
                ? `${Math.round(ev.cache_hit_rate * 100)}%` : "?";
            cacheTxt = ` · 缓存命中 <span class="token-num">${rate}</span>` +
                        ` (${ev.cache_hit_tokens}/${(ev.cache_hit_tokens || 0) + (ev.cache_miss_tokens || 0)})`;
        }
        ctx.tokenInfo.innerHTML =
                    `⚡ <span class="token-num">${ev.total_tokens}</span> tokens ` +
                    `(输入 ${ev.prompt_tokens} · 输出 ${ev.completion_tokens} · ${ev.steps} 步)` +
            cacheTxt;
        ctx.details.style.display = "block";

    } else if (ev.type === "phase") {
        // 多 Agent 阶段卡片（单 Agent 不会发这个事件）
        if (ev.status === "doing") createPhaseCard(ctx, ev.name);
        else updatePhaseStatus(ctx, ev.name, ev.status);

    } else if (ev.type === "plan_from_planner") {
        const card = ctx.bubble.parentElement
            .querySelector(".phase-card:last-of-type .phase-body");
        if (card) {
            const steps = (ev.plan && ev.plan.steps) || [];
            card.innerHTML = `
                        <div style="font-weight:600;margin-bottom:6px;">📋 任务计划</div>
                        <div class="planner-steps">
                            ${steps.map((x, i) => `<div class="planner-step">${i + 1}. ${escapeHtml(x)}</div>`).join("")}
                        </div>`;
        }

    } else if (ev.type === "review_result") {
        const card = ctx.bubble.parentElement
            .querySelector(".phase-card:last-of-type .phase-body");
        if (card) {
            const r = ev.result || {};
            const passedClass = r.passed === true ? "review-passed"
                : (r.passed === false ? "review-failed" : "review-unknown");
            const passedText = r.passed === true ? "✅ 通过"
                : (r.passed === false ? "❌ 未通过" : "⚠️ 审查未完成");
            const suggestions = (r.suggestions || [])
                .map(x => `<li>${escapeHtml(x)}</li>`).join("");
            card.innerHTML = `
                        <div class="${passedClass}">${passedText}</div>
                        <div class="review-reason">${escapeHtml(r.reason || "")}</div>
                        ${suggestions ? `<ul class="review-suggestions">${suggestions}</ul>` : ""}`;
        }

    } else if (ev.type === "tool_call") {
        run.lastResultEl = appendToolCall(ctx, ev.name, ev.args);
        // 记下本次碰过的文件 → 流式期间显示临时芯片
        collectProduced(ctx, ev.name, ev.args);

    } else if (ev.type === "tool_result") {
        if (run.lastResultEl) {
            updateToolResult(run.lastResultEl, ev.content);
        } else {
            // 续跑时后端只回 tool_result（tool_call 上一轮已渲染）
            const items = ctx.execList.querySelectorAll(".tool-result");
            if (items.length > 0) {
                items[items.length - 1].textContent = "📋 " + ev.content;
            }
        }

    } else if (ev.type === "run_started") {
        ctx.runIndex = ev.run_index;

    } else if (ev.type === "context_stats") {
        updateContextStats(ctx, ev);

    } else if (ev.type === "project_context") {
        appendProjectContextNote(ctx, ev);

    } else if (ev.type === "permission_denied") {
        appendPermissionDenied(ctx, ev.content);

    } else if (ev.type === "confirm_delete") {
        ctx.confirmToken = ev.confirm_token;   // 记住一次性令牌，确认时回传
        showDeleteConfirm(ctx, ev.path, run.task || "");

    } else if (ev.type === "confirm_write") {
        ctx.confirmToken = ev.confirm_token;
        showWriteConfirm(ctx, ev.path, ev.old_content, ev.new_content);

    } else if (ev.type === "confirm_command") {
        ctx.confirmToken = ev.confirm_token;
        showCommandConfirm(ctx, ev.cmd);

    } else if (ev.type === "plan_update") {
        renderPlan(ev.plan);

    } else if (ev.type === "cancelled") {
        // 停止：保留已产出的文本，并明确标注是用户主动中断
        flushTextRender(run);
        ctx.bubble.innerHTML = DOMPurify.sanitize(marked.parse(run.fullText));
        const stopTip = document.createElement("div");
        stopTip.className = "bubble";
        stopTip.style.cssText = "background:#fef3c7;color:#92400e;margin-top:6px;";
        stopTip.textContent = run.fullText
            ? "⏹ 已停止本次任务，上方为中断前已产出的内容"
            : "⏹ 已停止本次任务";
        ctx.bubble.parentElement.appendChild(stopTip);
        // 服务端已把断点存进 pending_state，这里给一个「继续」入口
        const contBtn = document.createElement("button");
        contBtn.className = "trash-btn restore";
        contBtn.style.marginTop = "6px";
        contBtn.textContent = "▶ 从这里继续";
        contBtn.onclick = () => {
            contBtn.remove();
            stopTip.textContent = "▶ 正在从中断处继续…";
            ctx.confirmToken = null;   // 中断续跑不需要确认令牌
            resumeRun(ctx);
        };
        ctx.bubble.parentElement.appendChild(contBtn);

    } else if (ev.type === "error") {
        ctx.bubble.style.color = "#b91c1c";
        ctx.bubble.textContent = "⚠️ " + ev.content;

    } else if (ev.type === "done") {
        flushTextRender(run);      // rAF 可能还没触发，补最后一次
        finalizeExecList(ctx);
        finishRoundCard(ctx, run.sessionId);
    }
}

/**
 * 读取并分发整个 SSE 流。
 *
 * ⚠️ 注意这里的 catch **必须打日志**。原来是空的 `catch (e) { }`，
 * 结果 resumeRun 里 `finishRoundCard(ctx, mySessionId)` 这个
 * ReferenceError（resumeRun 根本没有 mySessionId）被静默吞掉，
 * 表现为「续跑后轮次卡片不出现」，查起来全靠猜。
 */
async function consumeSseStream(res, run) {
    const reader = res.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";
    while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split("\n\n");
        buffer = parts.pop();
        for (const part of parts) {
            if (!part.startsWith("data: ")) continue;
            try {
                handleSseEvent(run, JSON.parse(part.slice(6).trim()));
            } catch (e) {
                // 绝不静默：这里藏过一次真 bug
                console.error("[SSE] 事件处理失败", e);
            }
        }
    }
}

/**
 * 把流式文本的重渲染合并到「每帧一次」。
 *
 * 后端现在约每 50ms 发一段增量。每段都做一次 `marked.parse(全文)` +
 * `DOMPurify.sanitize` + 整体替换 innerHTML 是浪费 —— 一帧内来几段
 * 也只需渲染最后一次。用 rAF 合并，长回答下能省掉大部分解析。
 */
function scheduleTextRender(run) {
    if (run._rafPending) return;
    run._rafPending = true;
    requestAnimationFrame(() => {
        run._rafPending = false;
        flushTextRender(run, true);
    });
}

/** 真正渲染。``follow`` 为真时顺带处理滚动跟随。 */
function flushTextRender(run, follow) {
    if (!run.isActive() || !run.ctx || !run.ctx.bubble) return;
    run.ctx.bubble.innerHTML = DOMPurify.sanitize(marked.parse(run.fullText));
    if (follow) followChat();
}

/** 构造一次运行的上下文，供 consumeSseStream 用。 */
function makeRunContext(sessionId, ctx, task) {
    return {
        sessionId,
        ctx,
        task: task || "",
        fullText: (sessionStates[sessionId] || {}).fullText || "",
        lastResultEl: null,
        isActive: () => currentSessionId === sessionId,
    };
}

async function resumeRun(ctx) {
    const sessionId = (ctx.sessionId != null) ? ctx.sessionId : currentSessionId;
    const isActive = () => (currentSessionId === sessionId);

    if (!sessionStates[sessionId]) sessionStates[sessionId] = {};
    const st = sessionStates[sessionId];
    st.status = "running";
    // 续跑同样是一次运行：显示「停止」按钮（必须在 status 置位之后调用）
    updateRunControls();

    let fullText = st.fullText || "";

    try {
        const body = {
            task: "",
            session_id: sessionId,
            resume: true,   // 关键：让后端从 pending state 续跑，而不是重新开始
            // 服务端签发的一次性确认令牌：没有它，后端会拒绝执行挂起的危险操作
            confirm_token: ctx.confirmToken || null,
            // 用 Executor 的 provider / model，保持与中断时一致
            provider_id: (multiAgentMode ? (roleSelection.executor.providerId || currentProviderId) : currentProviderId) || null,
            model: (multiAgentMode ? (roleSelection.executor.model || currentModelName) : currentModelName) || null,
            // 不再传 project_root：工作目录以「会话所属工作区」为准，
            // 由后端解析，避免前端全局 localStorage 覆盖其它会话的设置
        };
        if (multiAgentMode) {
            // 续跑时三角色的 provider / model 都要与中断前一致
            body.planner_provider_id = roleSelection.planner.providerId || currentProviderId;
            body.reviewer_provider_id = roleSelection.reviewer.providerId || currentProviderId;
            body.planner_model = roleSelection.planner.model;
            body.executor_model = roleSelection.executor.model;
            body.reviewer_model = roleSelection.reviewer.model;
        }

        // 多 Agent 模式续跑打 /run_multi（保留 Planner 上下文），否则打单 Agent /run
        const url = multiAgentMode ? "/run_multi" : "/run";
        const res = await fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body)
        });

        const run = makeRunContext(sessionId, ctx, null);
        await consumeSseStream(res, run);
        fullText = run.fullText;

        if (isActive()) {
            ctx.bubble.innerHTML = DOMPurify.sanitize(marked.parse(fullText));
            ctx.bubble.querySelectorAll('pre code').forEach(block => {
                hljs.highlightElement(block);
            });
        }
        await loadSessions();
    } catch (err) {
        if (isActive()) alert("继续请求失败：" + err.message);
    } finally {
        st.status = "done";
        if (st.hintEl) { st.hintEl.remove(); st.hintEl = null; }
        loadSessions();
        updateRunControls();   // 续跑结束：恢复「执行」、隐藏「停止」
        if (isActive()) {
            const input = document.getElementById("task");
            if (input) input.focus();
        }
    }
}

function showDeleteConfirm(ctx, path, originalTask) {
    // 移除已有的确认框
    const existing = ctx.bubble.parentElement.querySelector(".confirm-actions");
    if (existing) existing.remove();

    const div = document.createElement("div");
    div.className = "confirm-actions";
    div.innerHTML = `
                <span class="confirm-text">🗑 确认删除 <b>${escapeHtml(path)}</b>？</span>
                <button class="confirm-btn yes" title="确认删除">✓</button>
                <button class="confirm-btn no" title="取消">✗</button>
    `;

    div.querySelector(".confirm-btn.yes").addEventListener("click", () => {
        div.remove();
        // 用户确认后：续跑 Agent（复用 pending state，从原 step 继续）
        resumeRun(ctx);
    });

    div.querySelector(".confirm-btn.no").addEventListener("click", () => {
        div.remove();
        // 显示取消提示
        const cancelMsg = document.createElement("div");
        cancelMsg.className = "bubble";
        cancelMsg.style.background = "#f3f4f6";
        cancelMsg.style.color = "#6b7280";
        cancelMsg.style.marginTop = "6px";
        cancelMsg.textContent = "❌ 已取消删除";
        ctx.bubble.parentElement.appendChild(cancelMsg);
    });

    ctx.bubble.parentElement.appendChild(div);
    chat.scrollTop = chat.scrollHeight;
}

