/* EvalScope Perf Console — 表单核心：DOM 引用 / 动态行 / 配置读写 / 本地缓存 */
(function () {
'use strict';

const $ = (id) => document.getElementById(id);
const LS_KEY = 'evalscope_console_v3';

const DOM = {
    modelName: $('model_name'), apiUrl: $('api_url'), tokenizerPath: $('tokenizer_path'),
    apiKeyContainer: $('api-key-container'), keySummary: $('key-summary'),
    addApiKeyBtn: $('add-api-key-btn'),
    datasetName: $('openclaw_dataset_name'), datasetPath: $('openclaw_dataset_path'),
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

const createIORow = (i, o) => numRow('io-input', 'io-output', '输入', '输出', 0, i, o);
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
        if (!isNaN(a) && !isNaN(b)) io.push({ input: a, output: b });
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
        model_name: DOM.modelName.value.trim(),
        api_url: DOM.apiUrl.value.trim(),
        tokenizer_path: DOM.tokenizerPath.value.trim(),
        openai_api_keys: [...DOM.apiKeyContainer.querySelectorAll('.api-key-input')]
            .map((i) => i.value.trim()).filter(Boolean),
        openclaw_dataset_name: DOM.datasetName.value.trim() || 'line_by_line',
        openclaw_dataset_path: DOM.datasetPath.value.trim(),
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
    if (cfg.openclaw_dataset_name !== undefined) DOM.datasetName.value = cfg.openclaw_dataset_name || '';
    if (cfg.openclaw_dataset_path !== undefined) DOM.datasetPath.value = cfg.openclaw_dataset_path || '';
    if (cfg.stream !== undefined) DOM.streamMode.value = cfg.stream ? 'true' : 'false';

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
        cfg.input_output_groups.forEach((g) => DOM.ioContainer.appendChild(createIORow(g.input, g.output)));
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
    if (cfg.task_types.includes('openclaw') &&
        ['line_by_line', 'custom', 'custom_multi_turn'].includes(cfg.openclaw_dataset_name) &&
        !cfg.openclaw_dataset_path) {
        return fail('当前 dataset 需要填写数据集路径', DOM.datasetPath);
    }
    if (cfg.task_types.includes('manual') && !cfg.manual_dataset.length) {
        return fail('manual 任务至少需要一个问题');
    }
    if (!cfg.input_output_groups.length) return fail('IO 组合至少一行');
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
};
})();
