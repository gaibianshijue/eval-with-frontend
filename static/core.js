/* EvalScope Perf Console — 表单核心：DOM 引用 / 动态行 / 配置读写 / 本地缓存 */
(function () {
'use strict';

const $ = (id) => document.getElementById(id);
const LS_KEY = 'evalscope_console_v3';

const DOM = {
    apiProtocol: $('api_protocol'), protocolTip: $('protocol-tip'),
    multiTurn: $('multi_turn'), multiTurnBox: $('multi-turn-box'), mtPreview: $('mt-preview'),
    minTurns: $('min_turns'), maxTurns: $('max_turns'),
    firstTurnLength: $('first_turn_length'), subsequentTurnLength: $('subsequent_turn_length'),
    toolsEnabled: $('tools_enabled'), toolsBox: $('tools-box'), toolsJson: $('tools_json'),
    toolChoice: $('tool_choice'), toolsSample: $('btn-tools-sample'),
    datasetPresets: $('dataset-presets'),
    builtinDataset: $('builtin_dataset'), builtinTip: $('builtin-dataset-tip'),
    modelName: $('model_name'), apiUrl: $('api_url'), tokenizerPath: $('tokenizer_path'),
    apiKeyContainer: $('api-key-container'), keySummary: $('key-summary'),
    addApiKeyBtn: $('add-api-key-btn'),
    datasetName: $('dataset_name'), datasetPath: $('dataset_path'),
    manualContainer: $('manual-dataset-container'), addManualBtn: $('add-manual-btn'),
    manualCount: $('manual-count'),
    ioContainer: $('io-group-container'), crContainer: $('cr-group-container'),
    addIoBtn: $('add-io-btn'), addCrBtn: $('add-cr-btn'), btnCrFill: $('btn-cr-fill'),
    gridSummary: $('grid-summary'),
    testRepeats: $('test_repeats'), waitBetween: $('wait_between_tests'),
    logEveryN: $('log_every_n_query'), prefixLength: $('prefix_length'), streamMode: $('stream_mode'),
    toastWrap: $('toast-wrap'),
};

function toast(msg, type) {
    const el = document.createElement('div');
    el.className = 'toast' + (type ? ' ' + type : '');
    el.textContent = msg;
    DOM.toastWrap.appendChild(el);
    setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity .3s'; }, 3200);
    setTimeout(() => el.remove(), 3600);
}

let saveTimer = null;
function scheduleSave() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveToLS, 250);
}

/* ------------------------------------------------ 动态行 */
function mkEl(tag, cls, props) {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    Object.assign(el, props || {});
    return el;
}

function delBtn(onClick) {
    const b = mkEl('button', 'row-del', { type: 'button', title: '删除', textContent: '✕' });
    b.addEventListener('click', onClick);
    return b;
}

function createApiKeyRow(value) {
    const div = mkEl('div', 'row-item');
    const tag = mkEl('span', 'idx-tag');
    const input = mkEl('input', 'api-key-input', {
        type: 'password', placeholder: 'sk-...', value: value || '', autocomplete: 'off',
    });
    const eye = mkEl('button', 'row-del', { type: 'button', title: '显示 / 隐藏', textContent: '👁' });
    eye.addEventListener('click', () => {
        input.type = input.type === 'password' ? 'text' : 'password';
        eye.textContent = input.type === 'password' ? '👁' : '🙈';
    });
    const del = delBtn(() => {
        if (DOM.apiKeyContainer.querySelectorAll('.row-item').length > 1) {
            div.remove(); refreshKeys(); scheduleSave();
        } else {
            input.value = ''; refreshKeys();
            toast('至少保留一个 API Key 输入框');
        }
    });
    input.addEventListener('input', () => { refreshKeys(); scheduleSave(); });
    div.append(tag, input, eye, del);
    return div;
}

function refreshKeys() {
    DOM.apiKeyContainer.querySelectorAll('.row-item').forEach((row, i) => {
        const t = row.querySelector('.idx-tag');
        if (t) t.textContent = '#' + (i + 1);
    });
    const n = keyCount();
    DOM.keySummary.textContent = n + ' 个用户';
    updateGridSummary();
}

function keyCount() {
    return [...DOM.apiKeyContainer.querySelectorAll('.api-key-input')]
        .filter((i) => i.value.trim()).length;
}

function numRow(clsA, clsB, phA, phB, minA, valA, valB, extra) {
    const div = mkEl('div', 'row-item');
    const a = mkEl('input', clsA, { type: 'number', min: String(minA), placeholder: phA, value: valA ?? '' });
    const arrow = mkEl('span', 'arrow', { textContent: '→' });
    const b = mkEl('input', clsB, { type: 'number', min: String(minA), placeholder: phB, value: valB ?? '' });
    const del = delBtn(() => { div.remove(); updateGridSummary(); scheduleSave(); });
    [a, b].forEach((el) => el.addEventListener('input', () => { updateGridSummary(); scheduleSave(); }));
    div.append(a, arrow, b, del);
    if (extra) div.appendChild(extra);
    return div;
}

// 输入/输出留空 = 0 = 不限制（不传给 evalscope，用服务端默认值）
const createIORow = (i, o) => numRow('io-input', 'io-output', '输入（留空不限）', '输出（留空不限）', 0, i, o);
const createCRRow = (p, n) => numRow('cr-parallel', 'cr-number', '并发', '请求数', 1, p, n);

function createManualRow(qVal, aVal) {
    const div = mkEl('div', 'row-item');
    const q = mkEl('textarea', 'manual-q', { rows: 1, placeholder: '问题（必填）', value: qVal || '' });
    const a = mkEl('textarea', 'manual-a', { rows: 1, placeholder: '答案（选填）', value: aVal || '' });
    const del = delBtn(() => { div.remove(); refreshManual(); scheduleSave(); });
    [q, a].forEach((el) => {
        el.addEventListener('input', () => {
            el.style.height = 'auto';
            el.style.height = Math.min(el.scrollHeight, 120) + 'px';
            refreshManual(); scheduleSave();
        });
    });
    div.append(q, a, del);
    return div;
}

function refreshManual() {
    const n = [...DOM.manualContainer.querySelectorAll('.manual-q')]
        .filter((t) => t.value.trim()).length;
    DOM.manualCount.textContent = n;
    DOM.manualCount.classList.toggle('muted', n === 0);
}

/* ------------------------------------------------ 协议 / 多轮联动 */
const PROTOCOLS = {
    openai: {
        ph: 'http://127.0.0.1:8000/v1/chat/completions',
        tip: '标准 OpenAI 兼容端点，Authorization: Bearer <api_key>',
    },
    anthropic: {
        ph: 'https://api.anthropic.com/v1/messages',
        tip: 'Anthropic 原生 /v1/messages：x-api-key + anthropic-version，URL 填域名或 /v1 会自动补全',
    },
    openai_responses: {
        ph: 'http://127.0.0.1:8000/v1/responses',
        tip: 'OpenAI Responses API（--api openai_responses）',
    },
    dashscope: {
        ph: 'https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation',
        tip: 'DashScope 原生端点（--api dashscope）',
    },
};

function syncProtocol() {
    const p = DOM.apiProtocol.value || 'openai';
    const conf = PROTOCOLS[p] || PROTOCOLS.openai;
    DOM.apiUrl.placeholder = conf.ph;
    DOM.protocolTip.textContent = conf.tip;
}

function syncMultiTurn() {
    const on = DOM.multiTurn.checked;
    DOM.multiTurnBox.hidden = !on;
    updateMultiTurnPreview();
}

/* ------------------------------------------------ 多轮长度预估 */
function isTaskChecked(value) {
    const cb = document.querySelector(`.task-type-cb[value="${value}"]`);
    return !!(cb && cb.checked);
}

function fmtNum(n) {
    return Number(n).toLocaleString('en-US');
}

// 常见网关/推理服务的单请求上下文软上限；超过后容易出现 413 / 400 / 连接被断开
const CTX_WARN_TOKENS = 32768;

function firstRowValue(container, cls) {
    const row = container.querySelector('.row-item');
    if (!row) return 0;
    const el = row.querySelector('.' + cls);
    return el ? (parseInt(el.value, 10) || 0) : 0;
}

/**
 * 按 random_multi_turn 的真实语义实时算给用户看：
 * evalscope 每轮独立采样一条长度 ≈ min_prompt_length×prefix + input 的 user 消息，
 * 请求时把前面所有轮（user + 模型真实回复）拼成 context 一起发，所以第 n 轮 prompt ≈ n×input + (n-1)×output。
 */
function updateMultiTurnPreview() {
    const el = DOM.mtPreview;
    if (!el) return;
    if (!DOM.multiTurn.checked) {
        el.hidden = true;
        el.innerHTML = '';
        return;
    }
    el.hidden = false;

    const minT = Math.max(1, parseInt(DOM.minTurns.value, 10) || 1);
    const maxT = Math.max(minT, parseInt(DOM.maxTurns.value, 10) || minT);
    const isRandom = isTaskChecked('random');
    const isDataset = isTaskChecked('dataset');
    const prefix = parseInt(DOM.prefixLength.value, 10) || 0;
    const repeats = Math.max(1, parseInt(DOM.testRepeats.value, 10) || 1);
    const number = firstRowValue(DOM.crContainer, 'cr-number');
    const parallel = firstRowValue(DOM.crContainer, 'cr-parallel');
    const lines = [];

    if (!isRandom) {
        const name = DOM.datasetName.value.trim() || '多轮数据集';
        lines.push(
            `<b>${name}</b>：每轮 prompt 来自数据集自身文本，`
            + `输入/输出 token 组不参与构造；「最大轮数」${maxT} 只做截断，`
            + '「最小轮数」对数据集类多轮无效。'
        );
        lines.push('上下文同样逐轮累积（第 n 轮 = 前 n-1 轮的 user + 模型回复 + 本轮 user）。');
        if (isDataset && name && !isMultiTurnDataset(name)) {
            lines.push('<span class="mt-warn">该 dataset 不是 evalscope 的多轮数据集，需要改选多轮数据集才能跑多轮。</span>');
        }
        el.innerHTML = lines.join('<br>');
        return;
    }

    const inTok = firstRowValue(DOM.ioContainer, 'io-input');
    const outTok = firstRowValue(DOM.ioContainer, 'io-output');
    const perTurn = prefix + inTok;
    const avgT = (minT + maxT) / 2;
    const lastPrompt = maxT * perTurn + (maxT - 1) * outTok;
    const convs = number;
    const reqs = Math.round(convs * avgT * repeats);

    if (inTok <= 0) {
        lines.push(
            `<b>random_multi_turn</b>：每条对话 ${minT === maxT ? `${maxT} 轮` : `${minT}~${maxT} 轮`}，`
            + '每轮 prompt 长度取自「输入」列。'
        );
        lines.push('<span class="mt-warn">输入长度为空：random 任务必须填输入长度，否则校验不通过。</span>');
    } else {
        lines.push(
            `<b>random_multi_turn</b>：每条对话 ${minT === maxT ? `${maxT} 轮` : `${minT}~${maxT} 轮（平均 ${avgT}）`}，`
            + `每轮新增 prompt ≈ <b>${fmtNum(perTurn)} tok</b>`
            + (prefix > 0 ? `（= prefix ${fmtNum(prefix)} + 输入 ${fmtNum(inTok)}）` : '')
            + '。'
        );
        lines.push(
            `最长对话第 ${maxT} 轮请求的 prompt ≈ <span class="mt-key">${fmtNum(lastPrompt)} tok</span>`
            + `（${maxT} × ${fmtNum(perTurn)}`
            + (outTok > 0 ? ` + ${maxT - 1} × ${fmtNum(outTok)}` : ` + ${maxT - 1} 轮模型回复（长度未知）`)
            + '）。'
        );
        lines.push(
            `上下文是逐轮累积的：最长对话里是 <b>${maxT} 条各 ≈${fmtNum(perTurn)} tok 的 user 消息</b>，`
            + `<span class="mt-key">不是一次性 ${fmtNum(maxT * perTurn)} tok 的单条输入</span>。`
        );
        if (lastPrompt > CTX_WARN_TOKENS) {
            lines.push(
                `<span class="mt-warn">峰值上下文 ≈${fmtNum(lastPrompt)} tok，已超过常见上限 ${fmtNum(CTX_WARN_TOKENS)} tok：`
                + '网关/推理服务容易返回 413 / 400 或直接断连（表现为 http post fail / EOF），'
                + '建议降低输入长度或「最大轮数」，或减小并发。</span>'
            );
        }
    }
    if (inTok > 0) {
        if (outTok > 0) {
            lines.push(`每轮输出上限 ${fmtNum(outTok)} tok（请求带 <code>max_tokens</code>）。`);
        } else {
            lines.push(
                '<span class="mt-warn">输出未限制</span>：请求不带 <code>max_tokens</code>，回复长度由服务端决定，'
                + '实际上下文会比上表更大。'
            );
        }
    }
    if (convs > 0) {
        lines.push(
            `按第一条并发组（并发 ${parallel} / 请求数 ${convs}）：约 ${convs} 条对话 × 平均 ${avgT} 轮`
            + ` = ${fmtNum(Math.round(convs * avgT))} 次请求`
            + (repeats > 1 ? `，×${repeats} 次重复 = ${fmtNum(reqs)} 次` : '')
            + '（「请求数」在多轮下表示对话数）。'
        );
    }
    if (isDataset) {
        lines.push('<span class="mt-warn">同时勾选了 dataset 任务：会再按 dataset 自己的多轮数据跑一轮测试。</span>');
    }
    el.innerHTML = lines.join('<br>');
}

function syncTools() {
    DOM.toolsBox.hidden = !DOM.toolsEnabled.checked;
}

function parseTools() {
    const raw = DOM.toolsJson.value.trim();
    if (!raw) return { ok: false, error: '工具定义为空' };
    let arr;
    try {
        arr = JSON.parse(raw);
    } catch (e) {
        return { ok: false, error: `tools JSON 解析失败：${e.message}` };
    }
    if (!Array.isArray(arr)) arr = [arr];
    if (!arr.length) return { ok: false, error: 'tools 不能为空数组' };
    for (let i = 0; i < arr.length; i += 1) {
        const item = arr[i] || {};
        const fn = item.function && typeof item.function === 'object' ? item.function : item;
        if (!fn.name) return { ok: false, error: `tools[${i}] 缺少 function.name` };
    }
    return { ok: true, value: arr };
}

function fillDatasetPresets(list) {
    if (!DOM.datasetPresets || !Array.isArray(list)) return;
    DOM.datasetPresets.innerHTML = '';
    list.forEach((d) => {
        const opt = document.createElement('option');
        opt.value = d.name;
        opt.label = (d.label || d.name) + (d.needs_path ? ' · 需路径' : '');
        DOM.datasetPresets.appendChild(opt);
    });
}

/* ------------------------------------------------ 内置数据集（datasets/manifest.json） */
let builtinDatasets = [];

function fmtCount(n) {
    if (!n) return '0';
    if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
    if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
    return String(n);
}

// evalscope 里只有这些 dataset 走 run_multi_turn_benchmark；不勾 --multi-turn 会按单轮跑并报错
const MULTI_TURN_NAME_RE = /_multi_turn$|^(swe_smith|trie_.+)$/;

function isMultiTurnDataset(name) {
    if (!name) return false;
    if (MULTI_TURN_NAME_RE.test(name)) return true;
    return builtinDatasets.some((d) => d.name === name && d.multi_turn);
}

async function loadBuiltinDatasets() {
    if (!DOM.builtinDataset) return;
    try {
        const res = await fetch('/datasets');
        const data = await res.json();
        builtinDatasets = (data && data.datasets) || [];
        DOM.builtinDataset.dataset.source = (data && data.source_file) || '';
    } catch (_) {
        builtinDatasets = [];
    }
    DOM.builtinDataset.innerHTML = '<option value="">— 不使用（手动填写下方名称/路径）—</option>';
    builtinDatasets.forEach((d, i) => {
        const opt = document.createElement('option');
        opt.value = String(i);
        const scale = d.multi_turn ? `${d.turns} 轮` : `${d.rows} 条`;
        const size = d.bytes ? ` · ${(d.bytes / 1048576).toFixed(1)}MB` : '';
        opt.textContent = `${d.label || d.file}（${scale}${size}）`;
        DOM.builtinDataset.appendChild(opt);
    });

    // 首次打开（没有已保存的数据集路径）时默认选中多轮数据集，避免又跑回 random
    if (DOM.datasetPath && !DOM.datasetPath.value.trim() && builtinDatasets.length) {
        const idx = builtinDatasets.findIndex((d) => d.multi_turn);
        if (idx >= 0) {
            DOM.builtinDataset.value = String(idx);
            applyBuiltinDataset(String(idx));
        }
    }
}

function applyBuiltinDataset(idx) {
    const d = builtinDatasets[Number(idx)];
    if (!d) {
        if (DOM.builtinTip) {
            DOM.builtinTip.innerHTML = '来自 <code>datasets/manifest.json</code>，由 <code>python mock_dataset.py</code> 生成';
        }
        return;
    }
    if (DOM.builtinDataset) DOM.builtinDataset.value = String(idx);
    DOM.datasetName.value = d.name || '';
    DOM.datasetPath.value = d.path || '';
    // 内置数据集只跑 dataset 任务，避免和 random/manual 混着跑
    document.querySelectorAll('.task-type-cb').forEach((cb) => {
        cb.checked = cb.value === 'dataset';
        cb.closest('.chk').classList.toggle('checked', cb.checked);
    });
    DOM.multiTurn.checked = !!d.multi_turn;
    if (d.multi_turn && d.turns) DOM.maxTurns.value = Math.min(d.turns, 10);
    syncMultiTurn();
    // 数据集自带 prompt，输入/输出都默认留空（= 不限制，不传 evalscope 参数）
    if (!DOM.ioContainer.querySelectorAll('.row-item').length) {
        DOM.ioContainer.appendChild(createIORow('', ''));
    }
    DOM.ioContainer.querySelectorAll('.row-item').forEach((row) => {
        row.querySelector('.io-input').value = '';
        row.querySelector('.io-output').value = '';
    });
    if (DOM.builtinTip) {
        const bits = [`${d.label || d.file}`];
        bits.push(d.multi_turn ? `${d.turns} 轮对话` : `${d.rows} 条快照`);
        if (d.tool_results) bits.push(`单条最多 ${fmtCount(d.tool_results)} 个工具结果`);
        bits.push(`单条最大 ${fmtCount(d.max_chars)} 字符`);
        if (d.multi_turn && d.turns > 10) {
            bits.push(`已勾选多轮并截断为前 10 轮，想跑满请把「最大轮数」改成 ${d.turns}`);
        }
        DOM.builtinTip.textContent = bits.join('｜');
    }
    updateGridSummary();
    scheduleSave();
}

/* ------------------------------------------------ 网格摘要 */
function updateGridSummary() {
    const io = DOM.ioContainer.querySelectorAll('.row-item').length;
    const cr = DOM.crContainer.querySelectorAll('.row-item').length;
    const tt = document.querySelectorAll('.task-type-cb:checked').length;
    const rep = parseInt(DOM.testRepeats.value, 10) || 1;
    const keys = Math.max(keyCount(), 1);
    const total = io * cr * tt * rep * keys;
    DOM.gridSummary.textContent = total
        ? `${tt} 任务 × ${io} IO × ${cr} 并发 × ${rep} 重复 × ${keys} 用户 = ${total} 次测试`
        : '—';
    updateMultiTurnPreview();
}

function fillCRGroups(list) {
    DOM.crContainer.innerHTML = '';
    list.forEach((p) => DOM.crContainer.appendChild(createCRRow(p, p * 5)));
    updateGridSummary();
    scheduleSave();
}

/* ------------------------------------------------ 默认值 */
function initDynamicRows() {
    DOM.apiKeyContainer.appendChild(createApiKeyRow(''));
    DOM.ioContainer.appendChild(createIORow(32768, 500));
    [16, 32, 48, 64, 80, 96, 128, 144, 192, 256].forEach((p) =>
        DOM.crContainer.appendChild(createCRRow(p, p * 5)));
    DOM.manualContainer.appendChild(createManualRow('', ''));
    refreshKeys();
    refreshManual();
}

/* ------------------------------------------------ 收集 / 应用 */
function collectConfig() {
    const taskTypes = [...document.querySelectorAll('.task-type-cb:checked')].map((cb) => cb.value);
    const io = [];
    DOM.ioContainer.querySelectorAll('.row-item').forEach((row) => {
        const a = parseInt(row.querySelector('.io-input').value, 10);
        const b = parseInt(row.querySelector('.io-output').value, 10);
        // 留空按 0 处理，0 表示「不限制」
        io.push({ input: Number.isNaN(a) ? 0 : a, output: Number.isNaN(b) ? 0 : b });
    });
    const cr = [];
    DOM.crContainer.querySelectorAll('.row-item').forEach((row) => {
        const p = parseInt(row.querySelector('.cr-parallel').value, 10);
        const n = parseInt(row.querySelector('.cr-number').value, 10);
        if (!isNaN(p) && !isNaN(n)) cr.push({ parallel: p, number: n });
    });
    const manual = [];
    DOM.manualContainer.querySelectorAll('.row-item').forEach((row) => {
        const q = row.querySelector('.manual-q').value.trim();
        const a = row.querySelector('.manual-a').value.trim();
        if (q) manual.push({ question: q, answer: a });
    });
    return {
        api: DOM.apiProtocol.value || 'openai',
        multi_turn: DOM.multiTurn.checked,
        min_turns: parseInt(DOM.minTurns.value, 10) || 1,
        max_turns: parseInt(DOM.maxTurns.value, 10) || 2,
        first_turn_length: parseInt(DOM.firstTurnLength.value, 10) || 0,
        subsequent_turn_length: parseInt(DOM.subsequentTurnLength.value, 10) || 0,
        tools: DOM.toolsEnabled.checked ? DOM.toolsJson.value.trim() : '',
        tool_choice: DOM.toolsEnabled.checked ? DOM.toolChoice.value.trim() : '',
        model_name: DOM.modelName.value.trim(),
        api_url: DOM.apiUrl.value.trim(),
        tokenizer_path: DOM.tokenizerPath.value.trim(),
        openai_api_keys: [...DOM.apiKeyContainer.querySelectorAll('.api-key-input')]
            .map((i) => i.value.trim()).filter(Boolean),
        dataset_name: DOM.datasetName.value.trim() || 'line_by_line',
        dataset_path: DOM.datasetPath.value.trim(),
        test_repeats: parseInt(DOM.testRepeats.value, 10) || 1,
        wait_between_tests: parseInt(DOM.waitBetween.value, 10) || 0,
        log_every_n_query: parseInt(DOM.logEveryN.value, 10) || 100,
        prefix_length: parseInt(DOM.prefixLength.value, 10) || 0,
        stream: DOM.streamMode.value === 'true',
        task_types: taskTypes,
        input_output_groups: io,
        concurrency_request_groups: cr,
        manual_dataset: manual,
        evalscope_args: window.AdvPanel ? window.AdvPanel.getValues() : {},
    };
}

function applyConfig(cfg) {
    if (!cfg) return;
    if (cfg.model_name !== undefined) DOM.modelName.value = cfg.model_name || '';
    if (cfg.api_url !== undefined) DOM.apiUrl.value = cfg.api_url || '';
    if (cfg.tokenizer_path !== undefined) DOM.tokenizerPath.value = cfg.tokenizer_path || '';
    // 兼容早期 localStorage / 命令解析结果里的 openclaw_dataset_* 键名
    const dsName = cfg.dataset_name ?? cfg.openclaw_dataset_name;
    const dsPath = cfg.dataset_path ?? cfg.openclaw_dataset_path;
    if (dsName !== undefined) DOM.datasetName.value = dsName || '';
    if (dsPath !== undefined) DOM.datasetPath.value = dsPath || '';
    if (cfg.stream !== undefined) DOM.streamMode.value = cfg.stream ? 'true' : 'false';
    if (cfg.api !== undefined) DOM.apiProtocol.value = cfg.api || 'openai';
    if (cfg.multi_turn !== undefined) DOM.multiTurn.checked = !!cfg.multi_turn;
    if (cfg.min_turns !== undefined) DOM.minTurns.value = cfg.min_turns;
    if (cfg.max_turns !== undefined) DOM.maxTurns.value = cfg.max_turns;
    if (cfg.first_turn_length !== undefined) DOM.firstTurnLength.value = cfg.first_turn_length;
    if (cfg.subsequent_turn_length !== undefined) DOM.subsequentTurnLength.value = cfg.subsequent_turn_length;
    if (cfg.tools !== undefined) {
        const hasTools = Array.isArray(cfg.tools) ? cfg.tools.length > 0 : !!cfg.tools;
        DOM.toolsEnabled.checked = hasTools;
        DOM.toolsJson.value = hasTools
            ? (typeof cfg.tools === 'string' ? cfg.tools : JSON.stringify(cfg.tools, null, 2))
            : '';
    }
    if (cfg.tool_choice !== undefined) DOM.toolChoice.value = cfg.tool_choice || '';
    syncProtocol();
    syncMultiTurn();
    syncTools();

    if (Array.isArray(cfg.openai_api_keys) && cfg.openai_api_keys.length) {
        DOM.apiKeyContainer.innerHTML = '';
        cfg.openai_api_keys.forEach((k) => DOM.apiKeyContainer.appendChild(createApiKeyRow(k)));
    }
    if (Array.isArray(cfg.task_types)) {
        document.querySelectorAll('.task-type-cb').forEach((cb) => {
            cb.checked = cfg.task_types.includes(cb.value);
            cb.closest('.chk').classList.toggle('checked', cb.checked);
        });
    }
    if (Array.isArray(cfg.input_output_groups) && cfg.input_output_groups.length) {
        DOM.ioContainer.innerHTML = '';
        cfg.input_output_groups.forEach((g) =>
            DOM.ioContainer.appendChild(createIORow(g.input || '', g.output || '')));
    }
    if (Array.isArray(cfg.concurrency_request_groups) && cfg.concurrency_request_groups.length) {
        DOM.crContainer.innerHTML = '';
        cfg.concurrency_request_groups.forEach((g) =>
            DOM.crContainer.appendChild(createCRRow(g.parallel, g.number)));
    }
    if (Array.isArray(cfg.manual_dataset) && cfg.manual_dataset.length) {
        DOM.manualContainer.innerHTML = '';
        cfg.manual_dataset.forEach((m) => DOM.manualContainer.appendChild(createManualRow(m.question, m.answer)));
    }
    if (window.AdvPanel) window.AdvPanel.setValues(cfg.evalscope_args || {}, { replace: true });
    refreshKeys();
    refreshManual();
    updateGridSummary();
}

function validateConfig(cfg) {
    const fail = (msg, el) => {
        if (el) { el.classList.add('invalid'); el.focus(); }
        toast(msg, 'err');
        return msg;
    };
    [DOM.modelName, DOM.apiUrl, DOM.tokenizerPath, DOM.datasetPath].forEach((el) => el.classList.remove('invalid'));
    if (!cfg.model_name) return fail('请填写模型名称', DOM.modelName);
    if (!cfg.api_url) return fail('请填写 API URL', DOM.apiUrl);
    if (!cfg.openai_api_keys.length) return fail('请至少填写一个 API Key');
    if (!cfg.task_types.length) return fail('请至少选择一种任务类型');
    if (cfg.task_types.includes('random') && !cfg.tokenizer_path) {
        return fail('random 任务必须填写 tokenizer 路径', DOM.tokenizerPath);
    }
    if (cfg.task_types.includes('dataset') &&
        ['line_by_line', 'custom', 'custom_multi_turn', 'mock_messages'].includes(cfg.dataset_name) &&
        !cfg.dataset_path) {
        return fail('当前 dataset 需要填写数据集路径', DOM.datasetPath);
    }
    if (cfg.task_types.includes('manual') && !cfg.manual_dataset.length) {
        return fail('manual 任务至少需要一个问题');
    }
    if (cfg.task_types.includes('dataset') && !cfg.multi_turn && isMultiTurnDataset(cfg.dataset_name)) {
        return fail(`dataset=${cfg.dataset_name} 是多轮数据集，必须勾选「多轮对话压测 --multi-turn」`);
    }
    if (cfg.multi_turn) {
        if (cfg.min_turns < 1 || cfg.max_turns < cfg.min_turns) {
            return fail('多轮参数需满足 1 ≤ 最小轮数 ≤ 最大轮数', DOM.maxTurns);
        }
        if (cfg.task_types.includes('manual')) {
            return fail('多轮模式不支持 manual 任务，请改用 custom_multi_turn 数据集');
        }
        if (cfg.task_types.includes('random') && !cfg.tokenizer_path) {
            return fail('多轮 + random 需要 tokenizer 路径（random_multi_turn）', DOM.tokenizerPath);
        }
        if (cfg.task_types.includes('dataset') && cfg.dataset_name === 'custom_multi_turn'
            && !cfg.dataset_path) {
            return fail('custom_multi_turn 需要填写数据集路径', DOM.datasetPath);
        }
    }
    if (DOM.toolsEnabled.checked) {
        const parsed = parseTools();
        if (!parsed.ok) return fail(parsed.error, DOM.toolsJson);
    }
    if (!cfg.input_output_groups.length) {
        // 没填也行，后台按「不限制」兜底；只有 random 必须显式给输入长度
        cfg.input_output_groups.push({ input: 0, output: 0 });
    }
    if (cfg.task_types.includes('random') && !(cfg.input_output_groups[0].input > 0)) {
        return fail('random 任务需要填写输入长度（输出可以留空 = 不传 max_tokens）');
    }
    if (!cfg.concurrency_request_groups.length) return fail('并发组合至少一行');
    if (window.AdvPanel) {
        const err = window.AdvPanel.validate();
        if (err) { toast(err, 'err'); return err; }
    }
    return '';
}

/* ------------------------------------------------ 本地缓存 */
function saveToLS() {
    const cfg = collectConfig();
    cfg.cmd = $('cmd-input') ? $('cmd-input').value : '';
    try { localStorage.setItem(LS_KEY, JSON.stringify(cfg)); } catch (_) {}
}

function loadFromLS() {
    let data;
    try { data = JSON.parse(localStorage.getItem(LS_KEY)); } catch (_) { return false; }
    if (!data) return false;
    applyConfig(data);
    if (data.test_repeats) DOM.testRepeats.value = data.test_repeats;
    if (data.wait_between_tests !== undefined) DOM.waitBetween.value = data.wait_between_tests;
    if (data.log_every_n_query) DOM.logEveryN.value = data.log_every_n_query;
    if (data.prefix_length !== undefined) DOM.prefixLength.value = data.prefix_length;
    if (data.cmd && $('cmd-input')) $('cmd-input').value = data.cmd;
    updateGridSummary();
    return true;
}

window.Core = {
    DOM, toast, scheduleSave, createApiKeyRow, createIORow, createCRRow, createManualRow,
    refreshKeys, refreshManual, updateGridSummary, fillCRGroups, initDynamicRows,
    collectConfig, applyConfig, validateConfig, saveToLS, loadFromLS, keyCount,
    syncProtocol, syncMultiTurn, syncTools, parseTools, fillDatasetPresets,
    loadBuiltinDatasets, applyBuiltinDataset, updateMultiTurnPreview,
};
})();
