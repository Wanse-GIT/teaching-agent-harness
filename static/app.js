/**
 * 智能导学 Agent (Harness) - 前端交互（v5.0 多用户版）
 *
 * 学习体验：
 *  - SSE 流式输出（打字机效果 + 实时 Agent 协作流水线）
 *  - 步骤时间线 / 认知雷达图 / 策略效果曲线 / 学生画像
 *
 * v5.0 用户系统：
 *  - 登录/注册（JWT Bearer），画像与会话按用户隔离
 *  - 我的会话抽屉（切换/新建，切换时拉取消息历史）
 *  - 管理面板（用户管理 / 教学经验库 / 全局统计，仅管理员可见）
 */

// ============================================================
// State
// ============================================================

let conversationId = localStorage.getItem('tag_conv_id') || `c-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
let isStreaming = false;

// 认证状态
let currentUser = null;          // {id, username, role} | null
let authToken = localStorage.getItem('tag_token') || '';
let authMode = 'login';          // 'login' | 'register'

// ============================================================
// DOM
// ============================================================

const $ = (id) => document.getElementById(id);
const messagesEl = $('messages');
const welcomeEl = $('welcome');
const inputEl = $('input');
const sendBtn = $('sendBtn');
const composerEl = $('composer');
const restartBtn = $('restartBtn');
const phaseBadge = $('phaseBadge');
const routeReasonEl = $('routeReason');

// v5.0
const userChip = $('userChip'), userNameEl = $('userName'), userRoleEl = $('userRole');
const logoutBtn = $('logoutBtn'), adminBtn = $('adminBtn'), convBtn = $('convBtn');
const loginBtn = $('loginBtn');
const authOverlay = $('authOverlay'), authForm = $('authForm');
const authTitle = $('authTitle'), authSub = $('authSub');
const authUserEl = $('authUser'), authPassEl = $('authPass');
const authSubmit = $('authSubmit'), authSwitch = $('authSwitch'), authError = $('authError');
const authGuestLink = $('authGuestLink');
const convDrawer = $('convDrawer'), convDrawerMask = $('convDrawerMask'), convList = $('convList');
const adminPanel = $('adminPanel'), adminMask = $('adminMask');

// ============================================================
// Constants
// ============================================================

const PHASE_LABELS = {
    idle: '等待输入', routing: '调度中', diagnosing: '诊断分析中',
    assessing_step: '步骤评估中', guiding: '引导生成中',
    reflecting: '反思进化中', done: '完成',
};
const CLASS_META = {
    teaching: { icon: '📖', label: '教学性问题 · 示例复现' },
    problem_solving: { icon: '🔍', label: '解答性问题 · 苏格拉底提问' },
    factual: { icon: '⚡', label: '事实性问题 · 直接回答' },
};
const MODE_META = {
    example_reproduction: { icon: '📖', label: '示例复现' },
    socratic_questioning: { icon: '🔍', label: '苏格拉底提问' },
    direct_answer: { icon: '⚡', label: '直接回答' },
};
const LEVEL_LABELS = {
    remember: '记忆', understand: '理解', apply: '应用',
    analyze: '分析', evaluate: '评价', create: '创造',
};
const AGENT_LABELS = {
    supervisor: '调度官', diagnostician: '诊断师',
    step_assessor: '评估官', tutor: '导师', reflector: '反思师',
};

// ============================================================
// Auth & fetch wrapper（v5.0）
// ============================================================

function authHeaders() {
    return authToken ? { 'Authorization': `Bearer ${authToken}` } : {};
}

async function authFetch(url, opts = {}) {
    const headers = { ...(opts.headers || {}), ...authHeaders() };
    const resp = await fetch(url, { ...opts, headers });
    if (resp.status === 401) {
        // 令牌过期/失效 → 回登录页（保留当前会话 ID）
        showAuth();
        const err = await resp.json().catch(() => ({}));
        throw Object.assign(new Error(err.detail || '请先登录'), { auth: true });
    }
    return resp;
}

function showAuth() {
    authOverlay.style.display = 'flex';
}
function hideAuth() {
    authOverlay.style.display = 'none';
}

function setAuthMode(mode) {
    authMode = mode;
    const isLogin = mode === 'login';
    authTitle.textContent = isLogin ? '欢迎回来' : '创建账号';
    authSub.textContent = isLogin
        ? '登录后你的画像与学习记录将只属于你'
        : '注册后即可拥有独立的学习画像与经验积累';
    authSubmit.textContent = isLogin ? '登录' : '注册并进入';
    authSwitch.textContent = isLogin ? '注册一个' : '去登录';
    authError.style.display = 'none';
}

function renderUserChip() {
    if (currentUser) {
        userChip.style.display = 'inline-flex';
        userNameEl.textContent = currentUser.username;
        userRoleEl.textContent = currentUser.role === 'admin' ? '管理员' : '学习者';
        userRoleEl.className = `user-role ${currentUser.role}`;
        logoutBtn.style.display = '';
        loginBtn.style.display = 'none';
        adminBtn.style.display = currentUser.role === 'admin' ? '' : 'none';
        $('profileOwner').textContent = `· ${currentUser.username}`;
    } else {
        userChip.style.display = 'none';
        logoutBtn.style.display = 'none';
        loginBtn.style.display = '';
        adminBtn.style.display = 'none';
        $('profileOwner').textContent = '· 访客';
    }
}

async function bootAuth() {
    try {
        const resp = await fetch('/api/v2/auth/me', { headers: authHeaders() });
        const data = await resp.json();
        if (data.user) {
            currentUser = data.user;
            authToken = authToken || localStorage.getItem('tag_token') || '';
            hideAuth();
            renderUserChip();
            return;
        }
        // 无有效令牌：访客可继续（AUTH_REQUIRED=false），否则要求登录
        if (data.auth_required) showAuth();
    } catch {
        // 服务不可用：先放行（首页仍可渲染）
    }
    renderUserChip();
}

authForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    authError.style.display = 'none';
    authSubmit.disabled = true;
    try {
        const endpoint = authMode === 'login' ? '/api/v2/auth/login' : '/api/v2/auth/register';
        const resp = await fetch(endpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                username: authUserEl.value.trim(),
                password: authPassEl.value,
            }),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
        authToken = data.token;
        currentUser = data.user;
        localStorage.setItem('tag_token', authToken);
        hideAuth();
        renderUserChip();
        location.reload();   // 切换用户空间后重载，拉取该用户的会话列表与画像
    } catch (err) {
        authError.textContent = err.message;
        authError.style.display = '';
    } finally {
        authSubmit.disabled = false;
    }
});

authSwitch.addEventListener('click', (e) => {
    e.preventDefault();
    setAuthMode(authMode === 'login' ? 'register' : 'login');
});

authGuestLink.addEventListener('click', (e) => {
    e.preventDefault();
    authToken = '';
    currentUser = null;
    localStorage.removeItem('tag_token');
    hideAuth();
    renderUserChip();
});

logoutBtn.addEventListener('click', () => {
    authToken = '';
    currentUser = null;
    localStorage.removeItem('tag_token');
    location.reload();
});

loginBtn.addEventListener('click', () => {
    setAuthMode('login');
    showAuth();
});

// ============================================================
// Utils
// ============================================================

function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// 轻量 Markdown 渲染（代码块/行内码/加粗/列表/知识拓展卡片）
function renderMarkdown(text) {
    const codeBlocks = [];
    let src = String(text).replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
        codeBlocks.push({ lang: lang || 'code', code });
        return `\u0000CODE${codeBlocks.length - 1}\u0000`;
    });

    src = esc(src);
    src = src.replace(/`([^`\n]+)`/g, '<code class="inline">$1</code>');
    src = src.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    src = src.replace(/^- (.+)$/gm, '<li>$1</li>');
    src = src.replace(/(<li>[\s\S]*?<\/li>)/g, '<ul>$1</ul>').replace(/<\/ul>\s*<ul>/g, '');

    // 知识拓展块
    src = src.replace(/(📚\s*知识拓展：?[\s\S]*?)(?=($|\n[^📚]))/g, (m) => {
        const inner = m.replace(/\n/g, '<br>');
        return `<div class="knowledge">${inner}</div>`;
    });

    src = src.split(/\n{2,}/).map(p => /^\s*<(ul|div|pre)/.test(p) ? p : `<p>${p.replace(/\n/g, '<br>')}</p>`).join('');

    src = src.replace(/\u0000CODE(\d+)\u0000/g, (_, i) => {
        const { lang, code } = codeBlocks[i];
        return `<pre><span class="lang">${esc(lang)}</span><code>${esc(code.replace(/\n$/, ''))}</code></pre>`;
    });
    return src;
}

function scrollBottom() {
    messagesEl.scrollTop = messagesEl.scrollHeight;
}

function fmtTime(ts) {
    if (!ts) return '-';
    const d = new Date(ts * 1000);
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
    return sameDay ? hm : `${d.getMonth() + 1}/${d.getDate()} ${hm}`;
}

// ============================================================
// Chat rendering
// ============================================================

function hideWelcome() { if (welcomeEl) welcomeEl.remove(); }

function addUserMsg(content) {
    const div = document.createElement('div');
    div.className = 'msg msg-user';
    div.innerHTML = `<div class="bubble">${esc(content)}</div>`;
    messagesEl.appendChild(div);
    scrollBottom();
}

function newAssistantDraft() {
    const div = document.createElement('div');
    div.className = 'msg msg-assistant';
    div.innerHTML = `
        <div class="bubble-assist">
            <div class="md" data-role="draft"><span class="cursor"></span></div>
        </div>`;
    messagesEl.appendChild(div);
    scrollBottom();
    return div;
}

function finalizeAssistant(container, payload) {
    const mdEl = container.querySelector('[data-role="draft"]');
    let html = '';

    if (payload.step_progress && payload.current_step_title && payload.step_plan && !payload.step_plan.is_complete) {
        html += `<span class="step-chip">📌 ${payload.step_progress}：${esc(payload.current_step_title)}</span>`;
    }
    if (payload.is_continuation) {
        html += `<span class="cont-chip">↩ 继续之前的问题</span>`;
    }
    html += renderMarkdown(payload.reply);

    let meta = '';
    if (payload.guidance_mode) {
        const m = MODE_META[payload.guidance_mode] || { icon: '', label: payload.guidance_mode };
        meta += `<span class="meta-chip mode">${m.icon} ${esc(m.label)}</span>`;
    }
    if (payload.cognitive_level) {
        meta += `<span class="meta-chip">认知层级：${LEVEL_LABELS[payload.cognitive_level] || payload.cognitive_level}</span>`;
    }
    if (payload.step_progress) {
        meta += `<span class="meta-chip">步骤 ${payload.step_progress}</span>`;
    }

    mdEl.innerHTML = html;

    const assist = container.querySelector('.bubble-assist');
    if (payload.encouragement) {
        const e = document.createElement('div');
        e.className = 'encourage';
        e.innerHTML = `<span class="icon">💜</span><span>${esc(payload.encouragement)}</span>`;
        assist.appendChild(e);
    }
    if (meta) {
        const m = document.createElement('div');
        m.className = 'meta-row';
        m.innerHTML = meta;
        assist.appendChild(m);
    }
    scrollBottom();
}

// 历史消息渲染（会话切换时，v5.0）
function renderHistory(messages) {
    messagesEl.querySelectorAll('.msg').forEach(el => el.remove());
    if (!messages.length) return;
    messages.forEach(m => {
        if (m.role === 'user') {
            addUserMsg(m.content);
        } else {
            const div = document.createElement('div');
            div.className = 'msg msg-assistant';
            div.innerHTML = `<div class="bubble-assist"><div class="md">${renderMarkdown(m.content)}</div></div>`;
            messagesEl.appendChild(div);
        }
    });
    scrollBottom();
}

// ============================================================
// Dashboard renderers
// ============================================================

function setPhase(phase, active) {
    const label = PHASE_LABELS[phase] || phase;
    phaseBadge.innerHTML = `<i class="dot"></i>${esc(label)}`;
    phaseBadge.classList.toggle('active', !!active);
}

function setStage(agent, state, ms) {
    const el = document.querySelector(`.stage[data-agent="${agent}"]`);
    if (!el) return;
    el.classList.remove('active', 'done');
    if (state) el.classList.add(state);
    let msEl = el.querySelector('.ms');
    if (ms != null) {
        if (!msEl) { msEl = document.createElement('span'); msEl.className = 'ms'; el.appendChild(msEl); }
        msEl.textContent = `${ms}ms`;
    }
}

function resetPipeline() {
    document.querySelectorAll('.stage').forEach(el => {
        el.classList.remove('active', 'done');
        const ms = el.querySelector('.ms'); if (ms) ms.remove();
    });
    routeReasonEl.textContent = '等待调度…';
}

function updateClassification(payload) {
    if (!payload.question_type) return;
    const meta = CLASS_META[payload.question_type] || { icon: '❓', label: payload.question_type };
    $('classIcon').textContent = meta.icon;
    $('classLabel').textContent = meta.label;

    const emoEl = $('classEmotions');
    emoEl.innerHTML = '';
    const emotions = payload.detected_emotions || [];
    emotions.slice(0, 6).forEach(e => {
        const chip = document.createElement('span');
        chip.className = 'emotion-chip';
        chip.textContent = e;
        emoEl.appendChild(chip);
    });
    if (payload.is_continuation) {
        const chip = document.createElement('span');
        chip.className = 'emotion-chip';
        chip.textContent = '↩ 延续对话';
        emoEl.appendChild(chip);
    }
    $('reasoning').textContent = payload.classification_reasoning || '';
}

function updateSteps(stepPlan) {
    const card = $('stepCard');
    if (!stepPlan || !stepPlan.steps || !stepPlan.steps.length) { card.style.display = 'none'; return; }
    card.style.display = '';

    $('stepProgressBadge').textContent = stepPlan.is_complete
        ? '🎉 全部完成' : `${stepPlan.progress}`;

    const doneCount = stepPlan.completed_steps || 0;
    $('stepFill').style.width = `${Math.round(doneCount / stepPlan.total_steps * 100)}%`;

    const ol = $('stepTimeline');
    ol.innerHTML = stepPlan.steps.map(s => {
        const status = s.status === 'completed' ? 'completed'
            : s.status === 'in_progress' ? 'in_progress'
            : s.status === 'stuck' ? 'stuck' : '';
        const icon = s.status === 'completed' ? '✓' : s.status === 'in_progress' ? '●'
            : s.status === 'stuck' ? '!' : '○';
        return `<li class="${status}">
            <div class="tl-icon">${icon}</div>
            <div class="tl-body">
                <div class="tl-title">${esc(s.title)}</div>
                <div class="tl-desc">${esc(s.description || '')}</div>
                ${s.attempts > 1 ? `<div class="tl-attempts">已尝试 ${s.attempts} 次</div>` : ''}
            </div>
        </li>`;
    }).join('');
}

function updateProfile(profileSummary) {
    if (!profileSummary) return;
    const identity = profileSummary.split('\n').find(l => l.startsWith('用户身份：'));
    if (identity) {
        const idVal = identity.replace('用户身份：', '').trim();
        // v5.2 白手起家画像：身份未知时保留占位提示（后端摘要为「尚未了解…」）
        $('profileIdentity').textContent = idVal.includes('尚未了解')
            ? '尚未建立 · 随对话自动了解你' : idVal;
    }

    const tags = profileSummary.split('\n').find(l => l.startsWith('个性标签：'));
    if (tags) {
        $('profileTags').innerHTML = tags.replace('个性标签：', '').split(/[,，]/)
            .filter(t => t.trim()).map(t => `<span class="tag">${esc(t.trim())}</span>`).join('');
    }
    const grab = (prefix, groupId, chipsId, limit) => {
        const line = profileSummary.split('\n').find(l => l.startsWith(prefix));
        const group = $(groupId);
        if (!line) { group.style.display = 'none'; return; }
        const items = line.replace(prefix, '').split(/[,，]/).filter(t => t.trim()).slice(-limit);
        group.style.display = '';
        $(chipsId).innerHTML = items.map(t => `<span class="chip">${esc(t.trim())}</span>`).join('');
    };
    grab('已掌握知识点：', 'knownGroup', 'knownTopics', 8);
    grab('薄弱知识点：', 'weakGroup', 'weakTopics', 8);
    grab('感兴趣领域：', 'interestGroup', 'interestTopics', 5);
}

// ---- 认知雷达图 ----
function drawRadar(payload) {
    const canvas = $('radar');
    if (!payload.cognitive_level && !payload.profile_summary) return;
    const dpr = window.devicePixelRatio || 1;
    const W = canvas.clientWidth || 300, H = 210;
    canvas.width = W * dpr; canvas.height = H * dpr;
    const ctx = canvas.getContext('2d');
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, W, H);

    const values = radarValues(payload);
    const axes = ['参与度', '信心', '认知深度', '情绪稳定', '掌握广度'];
    const cx = W / 2, cy = H / 2 + 4, R = Math.min(W, H) / 2 - 26;
    const n = axes.length;

    ctx.strokeStyle = 'rgba(39, 49, 73, .9)';
    for (let ring = 1; ring <= 4; ring++) {
        ctx.beginPath();
        for (let i = 0; i <= n; i++) {
            const ang = -Math.PI / 2 + i * 2 * Math.PI / n;
            const r = R * ring / 4;
            const x = cx + r * Math.cos(ang), y = cy + r * Math.sin(ang);
            i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
        }
        ctx.stroke();
    }
    ctx.fillStyle = '#5d6579';
    ctx.font = '10.5px "Noto Sans SC", sans-serif';
    for (let i = 0; i < n; i++) {
        const ang = -Math.PI / 2 + i * 2 * Math.PI / n;
        ctx.beginPath();
        ctx.moveTo(cx, cy);
        ctx.lineTo(cx + R * Math.cos(ang), cy + R * Math.sin(ang));
        ctx.stroke();
        const lx = cx + (R + 13) * Math.cos(ang), ly = cy + (R + 12) * Math.sin(ang);
        ctx.textAlign = 'center';
        ctx.fillText(axes[i], lx, ly + 3);
    }
    ctx.beginPath();
    for (let i = 0; i <= n; i++) {
        const ang = -Math.PI / 2 + (i % n) * 2 * Math.PI / n;
        const r = R * values[i % n];
        const x = cx + r * Math.cos(ang), y = cy + r * Math.sin(ang);
        i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    ctx.closePath();
    const grad = ctx.createLinearGradient(0, 0, W, H);
    grad.addColorStop(0, 'rgba(240,179,92,.28)');
    grad.addColorStop(1, 'rgba(90,209,230,.28)');
    ctx.fillStyle = grad;
    ctx.fill();
    ctx.strokeStyle = 'rgba(240,179,92,.8)';
    ctx.lineWidth = 1.6;
    ctx.stroke();

    if (payload.cognitive_level) {
        $('cogLevel').textContent = LEVEL_LABELS[payload.cognitive_level] || payload.cognitive_level;
    }
}

function radarValues(payload) {
    const grab = (prefix, dft) => {
        const line = (payload.profile_summary || '').split('\n').find(l => l.startsWith(prefix));
        if (!line) return dft;
        const v = parseFloat(line.replace(prefix, '').match(/[\d.]+/));
        return isNaN(v) ? dft : v;
    };
    const engagement = grab('平均参与度：', 0.7);
    const frustration = grab('平均挫败感：', 0.0);
    const levelIdx = ['remember','understand','apply','analyze','evaluate','create'].indexOf(payload.cognitive_level || 'understand');
    const depth = 0.25 + (levelIdx >= 0 ? levelIdx : 1) * 0.15;
    const known = (payload.profile_summary || '').match(/已掌握知识点：([^【\n]*)/);
    const weak = (payload.profile_summary || '').match(/薄弱知识点：([^【\n]*)/);
    const knownN = known ? known[1].split(/[,，]/).filter(x => x.trim()).length : 0;
    const weakN = weak ? weak[1].split(/[,，]/).filter(x => x.trim()).length : 0;
    const breadth = knownN + weakN === 0 ? 0.2 : Math.min(1, knownN / Math.max(1, knownN + weakN));
    return [engagement, 0.7, depth, 1 - frustration, breadth];
}

// ---- 策略效果曲线 ----
function drawStrategyCurve(positiveCount) {
    const canvas = $('strategyChart');
    const dpr = window.devicePixelRatio || 1;
    const W = canvas.clientWidth || 300, H = 140;
    canvas.width = W * dpr; canvas.height = H * dpr;
    const ctx = canvas.getContext('2d');
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, W, H);

    const pts = [];
    for (let i = 0; i < Math.max(6, positiveCount * 2); i++) {
        const t = i / Math.max(6, positiveCount * 2 - 1);
        pts.push({ x: 10 + t * (W - 20), y: H - 18 - (0.15 + 0.65 * Math.pow(t, 0.7) + Math.sin(t * 9) * 0.05) * (H - 34) });
    }
    ctx.strokeStyle = 'rgba(39,49,73,.9)';
    ctx.beginPath(); ctx.moveTo(6, 6); ctx.lineTo(6, H - 14); ctx.lineTo(W - 4, H - 14); ctx.stroke();

    const grad = ctx.createLinearGradient(0, 0, W, 0);
    grad.addColorStop(0, 'rgba(90,209,230,.9)');
    grad.addColorStop(1, 'rgba(134,217,141,.9)');
    ctx.strokeStyle = grad; ctx.lineWidth = 2;
    ctx.beginPath();
    pts.forEach((p, i) => i === 0 ? ctx.moveTo(p.x, p.y) : ctx.lineTo(p.x, p.y));
    ctx.stroke();
    ctx.fillStyle = 'rgba(240,179,92,.9)';
    pts.filter((_, i) => i === pts.length - 1).forEach(p => {
        ctx.beginPath(); ctx.arc(p.x, p.y, 3.2, 0, Math.PI * 2); ctx.fill();
    });
    ctx.fillStyle = '#5d6579';
    ctx.font = '10px "Noto Sans SC", sans-serif';
    ctx.fillText('策略有效性 →', W / 2 - 30, H - 3);
}

// ============================================================
// SSE streaming
// ============================================================

let streamBuf = '';
let rafPending = false;
let streamDone = false;
function renderStream(draftEl) {
    if (rafPending || streamDone) return;
    rafPending = true;
    requestAnimationFrame(() => {
        rafPending = false;
        if (streamDone) return;
        draftEl.innerHTML = renderMarkdown(streamBuf) + '<span class="cursor"></span>';
        scrollBottom();
    });
}

async function sendMessage(content) {
    if (isStreaming || !content.trim()) return;
    isStreaming = true;
    sendBtn.disabled = true;
    hideWelcome();
    addUserMsg(content);
    resetPipeline();
    inputEl.value = '';
    autoResize();
    setPhase('routing', true);

    const draft = newAssistantDraft();
    const draftEl = draft.querySelector('[data-role="draft"]');
    draftEl.innerHTML = '<p class="thinking">思考中…</p><span class="cursor"></span>';
    streamBuf = '';
    streamDone = false;

    try {
        const resp = await authFetch(
            `/api/v2/conversations/${conversationId}/messages/stream`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ content }),
            }
        );
        if (!resp.ok || !resp.body) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.detail || `HTTP ${resp.status}`);
        }

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buf = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buf += decoder.decode(value, { stream: true });

            const parts = buf.split('\n\n');
            buf = parts.pop();
            for (const part of parts) {
                const ev = parseSSE(part);
                if (ev) handleEvent(ev, draftEl, draft);
            }
        }
    } catch (e) {
        if (!e.auth) {
            draftEl.innerHTML = `<p>⚠️ 连接出错：${esc(e.message)}。请确认服务已启动、LLM_API_KEY 已配置。</p>`;
        }
        setPhase('idle', false);
    } finally {
        streamDone = true;
        isStreaming = false;
        sendBtn.disabled = !inputEl.value.trim();
        document.querySelectorAll('.stage.active').forEach(el => el.classList.add('done'));
        setTimeout(refreshStrategyCard, 1500);
        setTimeout(refreshConvList, 2000);   // v5.0：新会话入列表
    }
}

function parseSSE(part) {
    let type = 'message', data = '';
    for (const line of part.split('\n')) {
        if (line.startsWith('event:')) type = line.slice(6).trim();
        else if (line.startsWith('data:')) data += line.slice(5).trim();
    }
    if (!data) return null;
    try { return { type, data: JSON.parse(data) }; }
    catch { return null; }
}

function handleEvent(ev, draftEl, draftContainer) {
    switch (ev.type) {
        case 'phase':
            setPhase(ev.data.phase, ev.data.phase !== 'done');
            break;
        case 'agent_enter': {
            setStage(ev.data.agent, 'active');
            if (ev.data.agent === 'reflector') setPhase('reflecting', true);
            break;
        }
        case 'agent_exit':
            setStage(ev.data.agent, 'done', ev.data.ms);
            break;
        case 'route':
            routeReasonEl.innerHTML =
                `<b style="color:var(--cyan)">调度官</b> → ${AGENT_LABELS[ev.data.to] || ev.data.to}
                 <span style="color:var(--faint)">(${ev.data.source === 'llm' ? 'LLM决策' : '规则决策'})</span>：
                 ${esc(ev.data.reason || '')}`;
            break;
        case 'token': {
            streamBuf += ev.data.delta;
            renderStream(draftEl);
            break;
        }
        case 'final': {
            streamBuf = '';
            const payload = ev.data.payload;
            finalizeAssistant(draftContainer, payload);
            updateClassification(payload);
            updateSteps(payload.step_plan);
            updateProfile(payload.profile_summary);
            drawRadar(payload);
            setPhase('done', false);
            break;
        }
        case 'error':
            draftEl.innerHTML = `<p>⚠️ ${esc(ev.data.message)}</p>`;
            setPhase('idle', false);
            break;
        case 'done':
            break;
    }
}

// ============================================================
// Strategy card
// ============================================================

async function refreshStrategyCard() {
    try {
        const [profileResp, healthResp] = await Promise.all([
            authFetch(`/api/v2/conversations/${conversationId}/profile`),
            fetch(`/api/v2/health`),
        ]);
        const profile = await profileResp.json();
        const p = profile.profile || {};
        const eff = (p.effective_strategies || []);
        const ineff = (p.ineffective_strategies || []);

        if (!eff.length && !ineff.length) {
            $('strategyCard').style.display = 'none';
            return;
        }
        $('strategyCard').style.display = '';
        const list = $('strategyList');
        list.innerHTML =
            eff.slice(-5).map(s => `<div class="strategy-item"><span>✔ ${esc(s)}</span><span class="val">有效</span></div>`).join('') +
            ineff.slice(-3).map(s => `<div class="strategy-item"><span>✘ ${esc(s)}</span><span class="val" style="color:var(--red)">待调整</span></div>`).join('');
        drawStrategyCurve(eff.length);
    } catch { /* ignore */ }
}

// ============================================================
// 我的会话抽屉（v5.0）
// ============================================================

async function refreshConvList() {
    try {
        const resp = await authFetch('/api/v2/conversations');
        const data = await resp.json();
        const items = data.conversations || [];
        if (!items.length) {
            convList.innerHTML = '<div class="conv-empty">还没有会话，发送第一条消息即会创建。</div>';
            return;
        }
        convList.innerHTML = items.map(c => `
            <div class="conv-item ${c.conversation_id === conversationId ? 'active' : ''}"
                 data-cid="${esc(c.conversation_id)}">
                <div class="conv-title">${esc(c.title || '新会话')}</div>
                <div class="conv-meta">
                    <span>${c.message_count} 条消息</span>
                    <span>${c.has_active_plan ? '📌 学习中' : fmtTime(c.updated_at)}</span>
                </div>
            </div>`).join('');
        convList.querySelectorAll('.conv-item').forEach(el => {
            el.addEventListener('click', () => switchConversation(el.dataset.cid));
        });
    } catch { /* ignore */ }
}

async function switchConversation(cid) {
    if (cid === conversationId) { toggleConvDrawer(false); return; }
    conversationId = cid;
    localStorage.setItem('tag_conv_id', conversationId);
    toggleConvDrawer(false);
    hideWelcome();
    streamBuf = '';

    // 拉取历史消息 + 会话摘要（步骤计划/画像）
    try {
        const [msgResp, sumResp] = await Promise.all([
            authFetch(`/api/v2/conversations/${cid}/messages`),
            authFetch(`/api/v2/conversations/${cid}`),
        ]);
        const msgData = await msgResp.json();
        renderHistory(msgData.messages || []);
        const sum = await sumResp.json();
        updateSteps(sum.step_plan);
        updateProfile(sum.user_profile && (sum.user_profile.identity !== undefined)
            ? null : null);
        // 画像摘要文本来自 profile 接口
        refreshProfileSummary();
    } catch { /* ignore */ }
    resetPipeline();
    setPhase('idle', false);
    refreshConvList();
}

async function refreshProfileSummary() {
    try {
        const resp = await authFetch(`/api/v2/conversations/${conversationId}/profile`);
        const data = await resp.json();
        if (data.summary) updateProfile(data.summary);
    } catch { /* ignore */ }
}

function toggleConvDrawer(show) {
    const visible = show !== undefined ? show : convDrawer.style.display === 'none';
    convDrawer.style.display = visible ? 'flex' : 'none';
    convDrawerMask.style.display = visible ? 'block' : 'none';
    if (visible) refreshConvList();
}

convBtn.addEventListener('click', () => toggleConvDrawer());
$('closeConvBtn').addEventListener('click', () => toggleConvDrawer(false));
convDrawerMask.addEventListener('click', () => toggleConvDrawer(false));

$('newConvBtn').addEventListener('click', () => {
    conversationId = `c-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
    localStorage.setItem('tag_conv_id', conversationId);
    messagesEl.querySelectorAll('.msg').forEach(el => el.remove());
    $('stepCard').style.display = 'none';
    toggleConvDrawer(false);
    location.reload();
});

// ============================================================
// 管理面板（v5.0，仅管理员）
// ============================================================

function toggleAdminPanel(show) {
    const visible = show !== undefined ? show : adminPanel.style.display === 'none';
    adminPanel.style.display = visible ? 'flex' : 'none';
    adminMask.style.display = visible ? 'block' : 'none';
    if (visible) loadAdminTab(document.querySelector('.admin-tab.active').dataset.tab);
}

adminBtn.addEventListener('click', () => toggleAdminPanel());
$('closeAdminBtn').addEventListener('click', () => toggleAdminPanel(false));
adminMask.addEventListener('click', () => toggleAdminPanel(false));

document.querySelectorAll('.admin-tab').forEach(tab => {
    tab.addEventListener('click', () => {
        document.querySelectorAll('.admin-tab').forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        document.querySelectorAll('.admin-tabpane').forEach(p => p.style.display = 'none');
        $(`tab-${tab.dataset.tab}`).style.display = '';
        loadAdminTab(tab.dataset.tab);
    });
});

function loadAdminTab(tab) {
    if (tab === 'users') loadUsers();
    else if (tab === 'insights') loadInsights();
    else if (tab === 'stats') loadStats();
}

async function adminAPI(path, opts = {}) {
    const resp = await authFetch(`/api/v2/admin${path}`, opts);
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
    return data;
}

async function loadUsers() {
    const tbody = $('usersTbody');
    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--faint)">加载中…</td></tr>';
    try {
        const data = await adminAPI('/users');
        tbody.innerHTML = (data.users || []).map(u => `
            <tr data-uid="${u.id}">
                <td>${esc(u.username)}</td>
                <td><span class="role-badge ${u.role}">${u.role === 'admin' ? '管理员' : '用户'}</span></td>
                <td>${u.conversation_count ?? 0}</td>
                <td>${u.is_active ? '<span style="color:var(--green)">正常</span>' : '<span style="color:var(--red)">已停用</span>'}</td>
                <td>${fmtTime(u.last_login_at)}</td>
                <td class="ops">
                    <button class="op-btn" data-op="toggle-role" title="切换角色">${u.role === 'admin' ? '降为用户' : '升为管理员'}</button>
                    <button class="op-btn" data-op="toggle-active">${u.is_active ? '停用' : '启用'}</button>
                    <button class="op-btn" data-op="reset-pw">重置密码</button>
                    <button class="op-btn danger" data-op="delete">删除</button>
                </td>
            </tr>`).join('');

        tbody.querySelectorAll('.op-btn').forEach(btn => {
            btn.addEventListener('click', () => adminUserOp(btn));
        });
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="6" style="text-align:center;color:var(--red)">${esc(e.message)}</td></tr>`;
    }
}

async function adminUserOp(btn) {
    const tr = btn.closest('tr');
    const uid = tr.dataset.uid;
    const op = btn.dataset.op;
    const name = tr.querySelector('td').textContent;
    try {
        if (op === 'toggle-role') {
            const cur = tr.querySelector('.role-badge').classList.contains('admin');
            await adminAPI(`/users/${uid}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ role: cur ? 'user' : 'admin' }),
            });
        } else if (op === 'toggle-active') {
            const isActive = tr.children[3].textContent.includes('正常');
            await adminAPI(`/users/${uid}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ is_active: !isActive }),
            });
        } else if (op === 'reset-pw') {
            const pw = prompt(`为「${name}」设置新密码（至少 6 位）：`);
            if (!pw) return;
            await adminAPI(`/users/${uid}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ password: pw }),
            });
            alert('密码已重置');
        } else if (op === 'delete') {
            if (!confirm(`确定删除「${name}」？其全部会话与画像将被级联清除，不可恢复。`)) return;
            await adminAPI(`/users/${uid}`, { method: 'DELETE' });
        }
        loadUsers();
    } catch (e) {
        alert(`操作失败：${e.message}`);
    }
}

$('createUserBtn').addEventListener('click', async () => {
    try {
        await adminAPI('/users', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                username: $('newUserName').value.trim(),
                password: $('newUserPass').value,
                role: $('newUserRole').value,
            }),
        });
        $('newUserName').value = ''; $('newUserPass').value = '';
        loadUsers();
    } catch (e) {
        alert(`创建失败：${e.message}`);
    }
});

async function loadInsights(filter = '') {
    const listEl = $('insightsList');
    listEl.innerHTML = '<div class="conv-empty">加载中…</div>';
    try {
        const q = filter ? `?topic=${encodeURIComponent(filter)}` : '';
        const data = await adminAPI(`/insights${q}`);
        const items = data.insights || [];
        if (!items.length) {
            listEl.innerHTML = '<div class="conv-empty">经验库暂无条目。教学对话反思评分达标后会自动沉淀到这里。</div>';
            return;
        }
        listEl.innerHTML = items.map(i => `
            <div class="insight-item">
                <div class="insight-head">
                    <span class="insight-topic">${esc(i.topic)}</span>
                    <span class="insight-mode">${esc(i.guidance_mode)}</span>
                    <span class="insight-eff" title="平均效果">★ ${(i.effectiveness * 100).toFixed(0)}%</span>
                    <span class="insight-n">${i.sample_count} 轮 · ${i.contributor_count} 位学习者贡献</span>
                    <button class="op-btn danger" data-iid="${esc(i.id)}">删除</button>
                </div>
                <div class="insight-text">${esc(i.insight)}</div>
            </div>`).join('');
        listEl.querySelectorAll('.op-btn').forEach(btn => {
            btn.addEventListener('click', async () => {
                if (!confirm('删除这条教学经验？')) return;
                try {
                    await adminAPI(`/insights/${encodeURIComponent(btn.dataset.iid)}`, { method: 'DELETE' });
                    loadInsights($('insightFilter').value.trim());
                } catch (e) { alert(`删除失败：${e.message}`); }
            });
        });
    } catch (e) {
        listEl.innerHTML = `<div class="conv-empty" style="color:var(--red)">${esc(e.message)}</div>`;
    }
}

$('insightFilterBtn').addEventListener('click', () => loadInsights($('insightFilter').value.trim()));
$('insightRefreshBtn').addEventListener('click', () => loadInsights(''));

async function loadStats() {
    const grid = $('statsGrid');
    grid.innerHTML = '<div class="conv-empty">加载中…</div>';
    try {
        const s = await adminAPI('/stats');
        const card = (label, value, sub) => `
            <div class="stat-card">
                <div class="stat-value">${value}</div>
                <div class="stat-label">${label}</div>
                ${sub ? `<div class="stat-sub">${sub}</div>` : ''}
            </div>`;
        grid.innerHTML =
            card('注册用户', s.users.total, `管理员 ${s.users.admins} · 活跃 ${s.users.active}`) +
            card('会话总数', s.conversations.total, `用户会话 ${s.conversations.by_users} · 访客 ${s.conversations.guest}`) +
            card('长期画像', s.memory.profiles, '按用户隔离') +
            card('教学经验', s.memory.teaching_insights, '跨用户统一整理') +
            card('策略统计', s.memory.strategy_stats, '全局共享') +
            card('持久化', s.db_persistent ? 'PostgreSQL' : '纯内存', s.db_persistent ? '已连接' : '降级模式');
    } catch (e) {
        grid.innerHTML = `<div class="conv-empty" style="color:var(--red)">${esc(e.message)}</div>`;
    }
}

// ============================================================
// Events
// ============================================================

function autoResize() {
    inputEl.style.height = 'auto';
    inputEl.style.height = Math.min(inputEl.scrollHeight, 130) + 'px';
    sendBtn.disabled = isStreaming || !inputEl.value.trim();
}

inputEl.addEventListener('input', autoResize);
inputEl.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage(inputEl.value);
    }
});
composerEl.addEventListener('submit', (e) => { e.preventDefault(); sendMessage(inputEl.value); });

document.querySelectorAll('.example-btn').forEach(btn => {
    btn.addEventListener('click', () => sendMessage(btn.dataset.q));
});

restartBtn.addEventListener('click', async () => {
    if (!confirm('重置会话将清空当前对话与学习进度（学生画像与策略经验会保留），确定吗？')) return;
    try {
        await authFetch(`/api/v2/conversations/${conversationId}`, { method: 'DELETE' });
    } catch { /* 服务不可用时也允许本地重置 */ }
    messagesEl.querySelectorAll('.msg').forEach(el => el.remove());
    streamBuf = '';
    location.reload();
});

// ============================================================
// Boot
// ============================================================

setPhase('idle', false);
autoResize();
setAuthMode('login');
bootAuth().then(() => {
    // 恢复已有会话：有历史则拉取（v5.0）
    authFetch(`/api/v2/conversations/${conversationId}/messages`)
        .then(r => r.json())
        .then(d => {
            if (d.messages && d.messages.length) {
                hideWelcome();
                renderHistory(d.messages);
                refreshProfileSummary();
            }
        })
        .catch(() => { /* ignore */ });
    refreshStrategyCard();
});
