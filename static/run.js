/* EvalScope Perf Console — 运行态：日志 / 结果表 / SSE / 进度 / 启停 */
(function () {
'use strict';

const C = window.Core;
const DOM = C.DOM;
const $ = (id) => document.getElementById(id);

const UI = {
    btnStart: $('btn-start'), btnStop: $('btn-stop'), btnRefresh: $('btn-refresh'),
    statePill: $('state-pill'), stateText: $('state-text'),
    progressFill: $('progress-fill'), progressText: $('progress-text'), progressPct: $('progress-pct'),
    progressEta: $('progress-eta'), progressDetail: $('progress-detail'), runId: $('run-id'),
    progressSubFill: $('progress-sub-fill'), progressSubText: $('progress-sub-text'),
    progressSubPct: $('progress-sub-pct'),
    statUsers: $('stat-users'), statDone: $('stat-done'), statRows: $('stat-rows'),
    logBox: $('log-box'), logCount: $('log-count'), logFilter: $('log-filter'), logAuto: $('log-autoscroll'),
    btnLogClear: $('btn-log-clear'),
    resultsCard: $('results-card'), resultsTbody: $('results-tbody'), resultsEmpty: $('results-empty'),
    resultsSummary: $('results-summary'),
    btnDownloadCsv: $('btn-download-csv'), btnCopyCsv: $('btn-copy-csv'),
    // 请求详情（benchmark_data.db）
    dbRun: $('db-run'), dbStatus: $('db-status'), dbQ: $('db-q'), dbSize: $('db-size'),
    dbStat: $('db-stat'), dbTbody: $('db-tbody'), dbEmpty: $('db-empty'),
    dbPrev: $('db-prev'), dbNext: $('db-next'), dbPageInfo: $('db-page-info'),
    dbRefresh: $('btn-db-refresh'), dbSummary: $('db-summary'),
    reqMask: $('req-mask'), reqTitle: $('req-title'), reqMeta: $('req-meta'),
    reqTabs: $('req-tabs'), reqBody: $('req-body'), reqClose: $('req-close'), reqCopy: $('req-copy'),
};

const TOOLS_SAMPLE = [{
    type: 'function',
    function: {
        name: 'get_weather',
        description: '查询指定城市的天气',
        parameters: {
            type: 'object',
            properties: {
                city: { type: 'string', description: '城市名' },
                date: { type: 'string', description: 'YYYY-MM-DD，默认今天' },
            },
            required: ['city'],
        },
    },
}];

/* ------------------------------------------------ 日志 */
const LOG_MAX = 3000;
let logLines = [];

function cssClass(text) {
    const t = (text || '').trim();
    if (!t) return '';
    if (t.startsWith('=== ')) return 'hl';
    if (t.startsWith('[fatal') || t.startsWith('[error') || /Traceback|Error:|错误/.test(t)) return 'err';
    if (t.startsWith('[warn') || t.includes('[warn]')) return 'warn';
    if (t.startsWith('[ok') || t.startsWith('[avg') || t.startsWith('✓')) return 'ok';
    if (t.startsWith('[user')) return 'user';
    if (t.startsWith('[')) return 'info';
    return '';
}

function makeLine(text) {
    const div = document.createElement('div');
    div.className = cssClass(text);
    div.textContent = text || ' ';
    return div;
}

function matches(text) {
    const f = UI.logFilter.value.trim().toLowerCase();
    return !f || (text || '').toLowerCase().includes(f);
}

function appendLog(text) {
    logLines.push(text);
    if (logLines.length > LOG_MAX) logLines = logLines.slice(-LOG_MAX);
    UI.logCount.textContent = logLines.length + ' 行';
    if (!matches(text)) return;
    UI.logBox.appendChild(makeLine(text));
    while (UI.logBox.childElementCount > LOG_MAX) UI.logBox.removeChild(UI.logBox.firstElementChild);
    if (UI.logAuto.checked) UI.logBox.scrollTop = UI.logBox.scrollHeight;
}

function rerenderLog() {
    UI.logBox.innerHTML = '';
    logLines.filter(matches).slice(-1000).forEach((t) => UI.logBox.appendChild(makeLine(t)));
    if (UI.logAuto.checked) UI.logBox.scrollTop = UI.logBox.scrollHeight;
}

function clearLog() {
    logLines = [];
    UI.logBox.innerHTML = '';
    UI.logCount.textContent = '0 行';
}

/* ------------------------------------------------ 结果表 */
const COLS = [
    ['api_key_index', '用户'], ['task_type', '任务'], ['Prompt_length', '输入'],
    ['Max_tokens', '输出'], ['Number_of_concurrency', '并发'], ['Total_requests', '请求'],
    ['Avg_Latency(s)', 'Latency Avg'], ['Latency_P90(s)', 'Latency P90'],
    ['TTFT_Avg(s)', 'TTFT Avg'], ['TTFT_P90(s)', 'TTFT P90'],
    ['TPOT_Avg(s)', 'TPOT Avg'], ['Avg_Inter_Token_Latency(s)', 'ITL Avg'],
    ['Avg_Input_Tokens', '实输入'], ['Avg_Output_Tokens', '实输出'], ['Avg_Turns', '轮数'],
    ['Output_through(tok/s)', '输出吞吐'], ['RPS(req/s)', 'RPS'],
    ['Total_token_through(tok/s)', '总吞吐'], ['repeat_num', '重复'],
];

const rendered = new Set();
let rowCount = 0;

function fmt(v) {
    if (v === '' || v === null || v === undefined) return '';
    const n = Number(v);
    if (!isFinite(n)) return String(v);
    const abs = Math.abs(n);
    if (abs >= 1000) return n.toFixed(0);
    if (abs >= 100) return n.toFixed(1);
    if (abs >= 1) return n.toFixed(3);
    return n.toFixed(4);
}

function addResultRow(row) {
    const sig = JSON.stringify(row);
    if (rendered.has(sig)) return;
    rendered.add(sig);

    UI.resultsCard.style.display = '';
    UI.resultsEmpty.style.display = 'none';

    const tr = document.createElement('tr');
    const keyIdx = parseInt(row.api_key_index, 10) || 1;
    tr.className = 'key-' + ((keyIdx - 1) % 8 + 1);
    if (row.repeat_num === 'AVG') tr.classList.add('avg');

    COLS.forEach(([col]) => {
        const td = document.createElement('td');
        let v = row[col];
        if (v === undefined || v === null) v = '';
        if (col === 'api_key_index') { v = '#' + keyIdx; td.classList.add('key-cell'); }
        else if (col === 'Total_requests') v = v + ' / ' + (row.Succeed_requests ?? '');
        else if (col !== 'task_type' && col !== 'repeat_num') v = fmt(v);
        td.textContent = v;
        tr.appendChild(td);
    });
    UI.resultsTbody.appendChild(tr);
    rowCount++;
    UI.statRows.textContent = rowCount;
    UI.resultsSummary.textContent = rowCount + ' 行';
    if (UI.logAuto.checked) {
        const wrap = UI.resultsTbody.closest('.results-scroll');
        if (wrap) wrap.scrollTop = wrap.scrollHeight;
    }
}

function resetResults() {
    rendered.clear();
    rowCount = 0;
    UI.resultsTbody.innerHTML = '';
    UI.statRows.textContent = '0';
    UI.resultsSummary.textContent = '';
    UI.resultsCard.style.display = 'none';
    UI.resultsEmpty.style.display = '';
}

function loadExistingResults() {
    fetch('/results').then((r) => r.json()).then((d) => {
        (d.results || []).forEach(addResultRow);
    }).catch(() => {});
}

function downloadCsv() {
    fetch('/download/csv').then((r) => {
        if (!r.ok) throw new Error('暂无结果文件');
        return r.blob();
    }).then((blob) => {
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url; a.download = 'results.csv';
        document.body.appendChild(a); a.click(); a.remove();
        URL.revokeObjectURL(url);
    }).catch((e) => C.toast('下载失败: ' + e.message, 'err'));
}

function copyTable() {
    const rows = [...UI.resultsTbody.querySelectorAll('tr')].map((tr) =>
        [...tr.children].map((td) => td.textContent.replace(/\s+/g, ' ').trim()).join('\t'));
    if (!rows.length) { C.toast('暂无结果可复制'); return; }
    const head = COLS.map((c) => c[1]).join('\t');
    navigator.clipboard.writeText([head].concat(rows).join('\n'))
        .then(() => C.toast('已复制 ' + rows.length + ' 行', 'ok'))
        .catch(() => C.toast('复制失败，请手动选择表格', 'err'));
}

/* ------------------------------------------------ SSE */
let sse = null;

function connectSSE() {
    if (sse) return;
    sse = new EventSource('/stream');
    sse.onmessage = (e) => {
        if (!e.data) return;
        if (e.data.startsWith('__RESULT__:')) {
            try { addResultRow(JSON.parse(e.data.slice(11))); } catch (_) {}
            return;
        }
        e.data.split('\r').filter(Boolean).forEach(appendLog);
    };
    sse.onerror = () => {};
}

function disconnectSSE() {
    if (sse) { sse.close(); sse = null; }
}

/* ------------------------------------------------ 进度 / 状态 */
let progressTimer = null;

function startProgressPoll() {
    stopProgressPoll();
    pollProgress();
    progressTimer = setInterval(pollProgress, 2000);
}

function stopProgressPoll() {
    if (progressTimer) { clearInterval(progressTimer); progressTimer = null; }
}

const STATE_TEXT = { idle: '空闲', running: '运行中', completed: '已完成', stopped: '已停止', error: '出错' };

let wasRunning = false;

function setRunning(running, state, error) {
    const s = running ? 'running' : (state || 'idle');
    UI.statePill.className = 'state-pill is-' + s;
    UI.stateText.textContent = STATE_TEXT[s] || s;
    UI.btnStart.disabled = running;
    UI.btnStop.disabled = !running;
    UI.progressFill.classList.toggle('active', running);
    if (error) UI.statePill.title = error; else UI.statePill.removeAttribute('title');
    if (wasRunning && !running) {
        // 跑完自动加载最新 db，省得手动刷新
        setTimeout(() => loadDbRuns(true), 800);
    }
    wasRunning = running;
}

async function pollProgress() {
    try {
        const data = await (await fetch('/status')).json();
        const f = data.fields || {};
        const cur = f._current || 0, total = f._total || 0, pct = f._percent || 0;
        UI.progressFill.style.width = pct + '%';
        UI.progressText.textContent = cur + ' / ' + total;
        UI.progressPct.textContent = '(' + pct + '%)';
        UI.progressEta.textContent = f['预计剩余'] || '--';
        UI.statDone.textContent = cur;
        // 子进度：当前子测试内的请求完成度（总进度条只表示任务个数）
        const subCur = f._sub_current || 0, subTotal = f._sub_total || 0, subPct = f._sub_percent || 0;
        UI.progressSubFill.style.width = (subTotal ? subPct : 0) + '%';
        UI.progressSubFill.classList.toggle('active', !!subTotal && !!data.running);
        UI.progressSubText.textContent = subTotal ? (subCur + ' / ' + subTotal) : '--';
        UI.progressSubPct.textContent = subTotal ? '请求 ' + subPct + '%' : '';
        UI.statUsers.textContent = f['用户数'] || 1;
        UI.runId.textContent = data.started_at ? '启动于 ' + data.started_at.replace('T', ' ') : '';
        const chips = [];
        if (f['任务']) chips.push('任务 ' + f['任务']);
        if (f['IO配置']) chips.push(f['IO配置']);
        if (f['并发']) chips.push(f['并发']);
        if (f['重复']) chips.push('重复 ' + f['重复']);
        if ((f['用户数'] || 1) > 1) chips.push(f['用户数'] + ' 用户并行');
        UI.progressDetail.innerHTML = '';
        chips.forEach((c) => {
            const s = document.createElement('span');
            s.className = 'chip';
            s.textContent = c;
            UI.progressDetail.appendChild(s);
        });
        setRunning(!!data.running, data.state, data.error);
    } catch (_) {}
}

async function pollState() {
    try {
        const data = await (await fetch('/state')).json();
        setRunning(!!data.running, data.state, data.error);
        if (data.running && !sse) { connectSSE(); startProgressPoll(); }
    } catch (_) {}
}

/* ------------------------------------------------ 启停 */
async function startRun() {
    if (window.AdvPanel) await window.AdvPanel.ready;
    const cfg = C.collectConfig();
    if (C.validateConfig(cfg)) return;
    C.saveToLS();

    clearLog();
    resetResults();
    UI.btnStart.disabled = true;
    appendLog('=== 正在提交配置... ===');
    try {
        const res = await fetch('/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(cfg),
        });
        const data = await res.json();
        if (!data.ok) {
            appendLog('[error] 启动失败: ' + data.error);
            C.toast(data.error, 'err');
            UI.btnStart.disabled = false;
            return;
        }
        appendLog('[info] 任务已提交: ' + data.run_id);
        disconnectSSE();
        connectSSE();
        startProgressPoll();
        await pollState();
        C.toast('压测已启动', 'ok');
    } catch (e) {
        appendLog('[error] 网络错误: ' + e.message);
        C.toast('网络错误: ' + e.message, 'err');
        UI.btnStart.disabled = false;
    }
}

async function stopRun() {
    UI.btnStop.disabled = true;
    appendLog('[user] 已请求停止 —— 等待当前 sub-test 结束');
    try { await fetch('/stop', { method: 'POST' }); } catch (_) {}
    setTimeout(pollState, 800);
}

/* ------------------------------------------------ 请求详情（benchmark_data.db） */
const DB = {
    runs: [], current: '', page: 1, pages: 1, size: 20, total: 0,
    detail: null, tab: 'req', lastData: null,
};

function fmtBytes(n) {
    const v = Number(n) || 0;
    if (v >= 1024 * 1024) return (v / 1024 / 1024).toFixed(1) + ' MB';
    if (v >= 1024) return (v / 1024).toFixed(1) + ' KB';
    return v + ' B';
}

async function loadDbRuns(selectLatest = false) {
    if (!UI.dbRun) return;
    try {
        const res = await fetch('/db/runs');
        const data = await res.json();
        if (!data.ok) return;
        DB.runs = data.runs || [];
        const sel = UI.dbRun;
        const prev = sel.value;
        sel.innerHTML = '';
        if (!DB.runs.length) {
            sel.innerHTML = '<option value="">（暂无 db 文件）</option>';
            DB.current = '';
            renderDbRecords(null);
            UI.dbStat.textContent = '';
            UI.dbSummary.textContent = '尚未产生 benchmark_data.db，跑一次压测后再刷新';
            return;
        }
        DB.runs.forEach((r) => {
            const opt = document.createElement('option');
            opt.value = r.id;
            opt.textContent = `${r.run_id} · ${r.label} · ${r.total} 条（失败 ${r.failed}）`;
            sel.appendChild(opt);
        });
        const wanted = selectLatest ? DB.runs[0].id : (prev || DB.runs[0].id);
        sel.value = Array.from(sel.options).some((o) => o.value === wanted) ? wanted : DB.runs[0].id;
        DB.current = sel.value;
        syncDbSummary();
        await loadDbRecords(1);
    } catch (_) { /* 忽略网络错误 */ }
}

function syncDbSummary() {
    const run = DB.runs.find((r) => r.id === DB.current);
    if (!run) return;
    UI.dbSummary.textContent =
        `${run.run_id} / ${run.label} · ${run.total} 条 · 成功 ${run.success} / 失败 ${run.failed} · ${fmtBytes(run.size)}`;
}

async function loadDbRecords(page = 1) {
    if (!DB.current) { renderDbRecords(null); return; }
    DB.page = Math.max(parseInt(page, 10) || 1, 1);
    DB.size = parseInt(UI.dbSize.value, 10) || 20;
    const params = new URLSearchParams({ db: DB.current, page: String(DB.page), size: String(DB.size) });
    const status = UI.dbStatus.value;
    const q = UI.dbQ.value.trim();
    if (status) params.set('status', status);
    if (q) params.set('q', q);
    try {
        const data = await (await fetch('/db/requests?' + params.toString())).json();
        if (!data.ok) { C.toast(data.error || '读取 db 失败', 'err'); return; }
        DB.lastData = data;
        DB.pages = data.pages || 1;
        DB.total = data.total || 0;
        syncDbStatusOptions(data.status_counts || {});
        renderDbRecords(data);
    } catch (e) {
        C.toast('网络错误: ' + e.message, 'err');
    }
}

function syncDbStatusOptions(counts) {
    const sel = UI.dbStatus;
    if (!sel) return;
    const prev = sel.value;
    const sum = Object.values(counts).reduce((a, b) => a + b, 0);
    const opts = [
        ['', `全部（${sum}）`],
        ['failed', '仅失败'],
        ['4xx', '4xx'],
        ['5xx', '5xx'],
    ];
    Object.keys(counts).filter((c) => c !== 'unknown').sort().forEach((code) => {
        opts.push([code, `${code}（${counts[code]}）`]);
    });
    if (counts.unknown) opts.push(['unknown', `未知响应码（${counts.unknown}）`]);
    sel.innerHTML = '';
    opts.forEach(([value, label]) => {
        const opt = document.createElement('option');
        opt.value = value;
        opt.textContent = label;
        sel.appendChild(opt);
    });
    sel.value = Array.from(sel.options).some((o) => o.value === prev) ? prev : '';
}

function renderDbRecords(data) {
    UI.dbTbody.innerHTML = '';
    if (!data || !data.items || !data.items.length) {
        UI.dbEmpty.style.display = '';
        UI.dbStat.textContent = data ? `共 ${data.total} 条，当前筛选命中 0 条` : '';
        UI.dbPageInfo.textContent = '第 1 / 1 页';
        UI.dbPrev.disabled = true;
        UI.dbNext.disabled = true;
        return;
    }
    UI.dbEmpty.style.display = 'none';
    UI.dbStat.textContent =
        `共 ${data.total} 条（成功 ${data.success} / 失败 ${data.failed}），当前筛选命中 ${data.matched} 条`;

    data.items.forEach((it) => {
        const tr = document.createElement('tr');
        if (!it.success) tr.classList.add('db-fail');
        tr.title = it.error || '点击查看请求体与响应';
        const cells = [
            '#' + it.id,
            it.status_code === null || it.status_code === undefined ? '—' : String(it.status_code),
            it.start_time,
            it.latency === null ? '' : it.latency + 's',
            it.ttft === null ? '' : it.ttft + 's',
            it.prompt_tokens ?? '',
            it.completion_tokens ?? '',
            it.turns || '',
            it.preview || '',
        ];
        cells.forEach((v, idx) => {
            const td = document.createElement('td');
            td.textContent = v;
            if (idx === 1) {
                td.classList.add('db-code');
                const code = Number(it.status_code);
                td.classList.add(it.success ? 'ok' : 'bad');
                if (code >= 400 && code < 500) td.classList.add('warn');
            }
            if (idx === 8) td.classList.add('db-preview');
            tr.appendChild(td);
        });
        tr.addEventListener('click', () => openRequestDetail(it.id));
        UI.dbTbody.appendChild(tr);
    });

    UI.dbPageInfo.textContent = `第 ${data.page} / ${data.pages} 页`;
    UI.dbPrev.disabled = data.page <= 1;
    UI.dbNext.disabled = data.page >= data.pages;
}

async function openRequestDetail(id) {
    if (!DB.current) return;
    const params = new URLSearchParams({ db: DB.current, id: String(id) });
    try {
        const data = await (await fetch('/db/request?' + params.toString())).json();
        if (!data.ok) { C.toast(data.error || '读取记录失败', 'err'); return; }
        DB.detail = data;
        DB.tab = 'req';
        UI.reqTitle.textContent = `#${data.id} · ${data.status_code ?? '—'}`;
        const parts = [
            `${data.start_time}`,
            `latency ${data.latency ?? '-'}s`,
            `ttft ${data.ttft ?? '-'}s`,
            `tokens ${data.prompt_tokens ?? '-'} / ${data.completion_tokens ?? '-'}`,
        ];
        if (data.turns) parts.push(`轮次 ${data.turns}`);
        if (data.trace_id) parts.push(`trace ${data.trace_id}`);
        UI.reqMeta.textContent = parts.join(' · ');
        UI.reqMask.hidden = false;
        renderDetailTab();
    } catch (e) {
        C.toast('网络错误: ' + e.message, 'err');
    }
}

function renderDetailTab() {
    UI.reqTabs.querySelectorAll('.tab').forEach((b) => {
        b.classList.toggle('is-active', b.dataset.tab === DB.tab);
    });
    const d = DB.detail;
    if (!d) { UI.reqBody.textContent = ''; return; }
    let content = '';
    if (DB.tab === 'req') {
        content = typeof d.request === 'string' ? d.request : JSON.stringify(d.request, null, 2);
    } else if (DB.tab === 'resp') {
        const reasoning = d.response?.reasoning || '';
        const text = d.response?.text || '';
        const blocks = [];
        if (reasoning) blocks.push(`--- 思考内容 ---\n${reasoning}`);
        blocks.push(text || '（无文本内容）');
        content = blocks.join('\n\n');
        if (d.error) content += `\n\n--- error ---\n${d.error}`;
    } else if (DB.tab === 'headers') {
        const h = d.response_headers || {};
        const keys = Object.keys(h);
        content = keys.length
            ? keys.map((k) => `${k}: ${h[k]}`).join('\n')
            : '（该记录没有响应头）\n'
              + '只有 Anthropic 协议会自动记录；补列之前产生的历史记录也不会有。';
    } else if (DB.tab === 'events') {
        content = JSON.stringify(d.response?.events || [], null, 2);
    } else {
        content = JSON.stringify(d.response?.tool_calls || [], null, 2);
        if (!content || content === '[]') content = '（该响应没有 tool_calls）';
    }
    UI.reqBody.textContent = content;
}

function closeRequestDetail() {
    UI.reqMask.hidden = true;
    DB.detail = null;
}

/* ------------------------------------------------ 事件绑定 */
function bind() {
    UI.btnStart.addEventListener('click', startRun);
    UI.btnStop.addEventListener('click', stopRun);
    UI.btnRefresh.addEventListener('click', async () => {
        await pollState(); await pollProgress();
        C.toast('状态已刷新');
    });

    DOM.addApiKeyBtn.addEventListener('click', () => {
        const row = C.createApiKeyRow('');
        DOM.apiKeyContainer.appendChild(row);
        C.refreshKeys();
        row.querySelector('input').focus();
        C.scheduleSave();
    });
    DOM.addIoBtn.addEventListener('click', () => {
        DOM.ioContainer.appendChild(C.createIORow(1024, 256));
        C.updateGridSummary(); C.scheduleSave();
    });
    DOM.addCrBtn.addEventListener('click', () => {
        DOM.crContainer.appendChild(C.createCRRow(8, 40));
        C.updateGridSummary(); C.scheduleSave();
    });
    DOM.btnCrFill.addEventListener('click', () => {
        const raw = prompt('输入并发度序列（逗号分隔），每项 ×5 作为请求数：', '16,32,64,128,256');
        if (!raw) return;
        const list = raw.split(',').map((s) => parseInt(s.trim(), 10)).filter((n) => n > 0);
        if (!list.length) { C.toast('未解析到有效并发度', 'err'); return; }
        C.fillCRGroups(list);
    });
    DOM.addManualBtn.addEventListener('click', () => {
        DOM.manualContainer.appendChild(C.createManualRow('', ''));
        C.refreshManual(); C.scheduleSave();
    });

    document.querySelectorAll('.task-type-cb').forEach((cb) => {
        cb.closest('.chk').classList.toggle('checked', cb.checked);
        cb.addEventListener('change', () => {
            cb.closest('.chk').classList.toggle('checked', cb.checked);
            const manualSect = document.getElementById('manual-sect');
            if (cb.value === 'manual' && cb.checked && manualSect) manualSect.open = true;
            C.updateGridSummary(); C.scheduleSave();
        });
    });

    if (DOM.builtinDataset) {
        DOM.builtinDataset.addEventListener('change', () => {
            C.applyBuiltinDataset(DOM.builtinDataset.value);
        });
    }

    DOM.apiProtocol.addEventListener('change', () => { C.syncProtocol(); C.scheduleSave(); });
    DOM.multiTurn.addEventListener('change', () => {
        C.syncMultiTurn();
        C.updateGridSummary();
        C.scheduleSave();
    });
    [DOM.minTurns, DOM.maxTurns, DOM.firstTurnLength, DOM.subsequentTurnLength,
        DOM.prefixLength].forEach((el) => {
        if (!el) return;
        el.addEventListener('input', () => { C.updateMultiTurnPreview(); C.scheduleSave(); });
        el.addEventListener('change', () => { C.updateMultiTurnPreview(); C.scheduleSave(); });
    });

    DOM.toolsEnabled.addEventListener('change', () => {
        C.syncTools();
        C.scheduleSave();
    });
    [DOM.toolsJson, DOM.toolChoice].forEach((el) => {
        el.addEventListener('input', () => C.scheduleSave());
        el.addEventListener('change', () => C.scheduleSave());
    });
    DOM.toolsSample.addEventListener('click', () => {
        DOM.toolsJson.value = JSON.stringify(TOOLS_SAMPLE, null, 2);
        if (!DOM.toolChoice.value.trim()) DOM.toolChoice.value = 'auto';
        C.scheduleSave();
        C.toast('已填入示例工具定义');
    });

    // 请求详情
    UI.dbRefresh.addEventListener('click', () => loadDbRuns(false));
    UI.dbRun.addEventListener('change', async () => {
        DB.current = UI.dbRun.value;
        syncDbSummary();
        await loadDbRecords(1);
    });
    UI.dbStatus.addEventListener('change', () => loadDbRecords(1));
    UI.dbSize.addEventListener('change', () => loadDbRecords(1));
    let dbTimer = null;
    UI.dbQ.addEventListener('input', () => {
        clearTimeout(dbTimer);
        dbTimer = setTimeout(() => loadDbRecords(1), 320);
    });
    UI.dbPrev.addEventListener('click', () => loadDbRecords(DB.page - 1));
    UI.dbNext.addEventListener('click', () => loadDbRecords(DB.page + 1));
    UI.reqTabs.addEventListener('click', (e) => {
        const btn = e.target.closest('.tab');
        if (!btn) return;
        DB.tab = btn.dataset.tab;
        renderDetailTab();
    });
    UI.reqClose.addEventListener('click', closeRequestDetail);
    UI.reqMask.addEventListener('click', (e) => {
        if (e.target === UI.reqMask) closeRequestDetail();
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && !UI.reqMask.hidden) closeRequestDetail();
    });
    UI.reqCopy.addEventListener('click', async () => {
        const text = UI.reqBody.textContent || '';
        try {
            await navigator.clipboard.writeText(text);
            C.toast('已复制当前标签内容');
        } catch (_) {
            C.toast('复制失败，请手动选择', 'err');
        }
    });

    ['#model_name', '#api_url', '#tokenizer_path', '#openclaw_dataset_name',
     '#openclaw_dataset_path', '#test_repeats', '#wait_between_tests',
     '#log_every_n_query', '#prefix_length', '#stream_mode'].forEach((sel) => {
        const el = document.querySelector(sel);
        if (!el) return;
        el.addEventListener('input', () => { C.updateGridSummary(); C.scheduleSave(); });
        el.addEventListener('change', () => { C.updateGridSummary(); C.scheduleSave(); });
    });

    UI.logFilter.addEventListener('input', rerenderLog);
    UI.logAuto.addEventListener('change', () => {
        if (UI.logAuto.checked) UI.logBox.scrollTop = UI.logBox.scrollHeight;
    });
    UI.btnLogClear.addEventListener('click', clearLog);
    UI.btnDownloadCsv.addEventListener('click', downloadCsv);
    UI.btnCopyCsv.addEventListener('click', copyTable);

    const obs = new MutationObserver(() => { C.refreshKeys(); C.scheduleSave(); });
    [DOM.apiKeyContainer, DOM.ioContainer, DOM.crContainer].forEach((c) =>
        obs.observe(c, { childList: true }));
}

/* ------------------------------------------------ init */
window.Run = {
    appendLog, addResultRow, pollState, pollProgress, bind, loadExistingResults, resetResults,
    loadDbRuns, loadDbRecords, openRequestDetail,
};

document.addEventListener('DOMContentLoaded', async () => {
    C.initDynamicRows();
    C.loadFromLS();
    C.syncProtocol();
    C.syncMultiTurn();
    C.syncTools();
    await C.loadBuiltinDatasets();
    bind();
    if (window.AdvPanel) {
        await window.AdvPanel.ready;
        if (window.AdvPanel.init) window.AdvPanel.init(C);
    }
    pollState();
    pollProgress();
    loadExistingResults();
    loadDbRuns(false);
});
})();
