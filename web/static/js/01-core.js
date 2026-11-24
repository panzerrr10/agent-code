// 批次 5：清理旧的 apiSettings（批次 4 前的设置弹窗残留）
try {
    if (localStorage.getItem("apiSettings")) {
        localStorage.removeItem("apiSettings");
        console.log("[Cleanup] 已移除旧的 apiSettings");
    }
} catch (e) {
    // localStorage 不可用时忽略
}

let multiAgentMode = false;
const chat = document.getElementById("chat");
let currentSessionId = null;

// 每个会话的运行状态：{ sessionId: { status: "running"|"done", fullText, plan, ctx } }
// fullText：本会话后台任务已接收的文本（切回时用它重绘气泡）
// ctx：后台任务的渲染上下文，切回时通过 Object.assign 重新指向新的 DOM
const sessionStates = {};

// ===== 会话级用量（总 token + 缓存命中率）=====
//
// 为什么要单独记一份：消息气泡里的 token 行是**单轮**的，而用户想知道
// 「这个会话到现在一共花了多少、缓存命中得怎么样」。
// sessionUsage 跨轮累加，切会话/刷新后从历史消息里重建。
//
// 缓存口径与后端一致：命中率 = 命中 tokens / **输入 tokens**。
// 没上报缓存字段的厂商（百炼/智谱可能没有）记 hasCache=false，
// 界面显示「未上报」而不是 0% —— 那是两回事。
const sessionUsage = {};

function emptyUsage() {
    return { total: 0, prompt: 0, completion: 0,
             hit: 0, miss: 0, cachePrompt: 0, hasCache: false };
}

function resetUsage(sessionId) {
    sessionUsage[sessionId] = emptyUsage();
}

function addUsage(sessionId, ev) {
    const u = sessionUsage[sessionId] || (sessionUsage[sessionId] = emptyUsage());
    u.total += ev.total_tokens || 0;
    u.prompt += ev.prompt_tokens || 0;
    u.completion += ev.completion_tokens || 0;
    const hit = ev.cache_hit_tokens || 0;
    const miss = ev.cache_miss_tokens || 0;
    if (hit || miss) {
        u.hasCache = true;
        u.hit += hit;
        u.miss += miss;
        // 只在「有缓存数据的那几轮」里累加分母，避免混进没上报的轮次
        u.cachePrompt += ev.prompt_tokens || 0;
    }
}

// 从历史消息重建（渲染前调用；用的是**全量** messages，不是只渲染的那 30 条）
function rebuildUsageFromHistory(sessionId, messages) {
    const u = emptyUsage();
    (messages || []).forEach(m => {
        if (m.role !== "assistant") return;
        try {
            const o = JSON.parse(m.content);
            const t = o && o.tokens;
            if (!t || !t.total_tokens) return;
            u.total += t.total_tokens || 0;
            u.prompt += t.prompt_tokens || 0;
            u.completion += t.completion_tokens || 0;
            const hit = t.cache_hit_tokens || 0;
            const miss = t.cache_miss_tokens || 0;
            if (hit || miss) {
                u.hasCache = true;
                u.hit += hit;
                u.miss += miss;
                u.cachePrompt += t.prompt_tokens || 0;
            }
        } catch (e) { /* 非 JSON 或旧格式，跳过 */ }
    });
    sessionUsage[sessionId] = u;
}

function formatUsage(u) {
    if (!u || !u.total) return "本会话：暂无用量";
    const parts = [`⚡ <span class="stat-num">${u.total.toLocaleString()}</span> tokens`];
    if (u.hasCache && u.cachePrompt > 0) {
        const pct = Math.round(u.hit / u.cachePrompt * 100);
        parts.push(`缓存命中 <span class="stat-num">${pct}%</span>` +
                           ` (${u.hit.toLocaleString()}/${u.cachePrompt.toLocaleString()})`);
    } else {
        parts.push("缓存 未上报");
    }
    return "本会话：" + parts.join('<span class="stat-sep">·</span>');
}

// 只更新「当前正在看的那个会话」显示；后台会话的记录留在 sessionUsage 里
function renderSessionStats(sessionId) {
    const el = document.getElementById("sessionStatsText");
    if (!el) return;
    if (sessionId && sessionId !== currentSessionId) return;
    const sid = sessionId || currentSessionId;
    el.innerHTML = formatUsage(sessionUsage[sid]);
}

