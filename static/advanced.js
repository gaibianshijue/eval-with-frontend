/* EvalScope Perf Console — 高级参数面板（由 evalscope schema 动态生成）+ 命令解析 */
(function () {
'use strict';

const $ = (id) => document.getElementById(id);
const GROUPS = $('adv-groups');
const SET_COUNT = $('adv-set-count');
const ONLY_SET = $('adv-only-set');
const SCHEMA_DESC = $('schema-desc');
const MANAGED_NOTE = $('managed-note');

let core = null;
let schema = null;
const meta = {};      // name -> field meta
const ctrls = {};     // name -> { el, field, row }
let managedFields = [];

/* ------------------------------------------------ 渲染 */
function placeholderOf(field) {
    const d = field.default;
    if (d === null || d === undefined) return '默认';
    if (typeof d === 'object') {
        const s = JSON.stringify(d);
        return s.length > 60 ? '默认: {...}' : '默认: ' + s;
    }
    return '默认: ' + String(d);
}

function buildCtrl(field) {
    const wrap = document.createElement('div');
    wrap.className = 'ctrl';
    let el;

    if (Array.isArray(field.choices) && field.choices.length) {
        el = document.createElement('select');
        const none = document.createElement('option');
        none.value = '';
        none.textContent = '默认' + (field.default ? ' (' + field.default + ')' : '');
        el.appendChild(none);
        field.choices.forEach((c) => {
            const o = document.createElement('option');
            o.value = String(c);
            o.textContent = String(c);
            el.appendChild(o);
        });
        if (field.kind === 'int') el.dataset.cast = 'int';
        if (field.kind === 'float') el.dataset.cast = 'float';
        if (field.kind === 'bool') el.dataset.cast = 'bool';
    } else if (field.kind === 'bool') {
        el = document.createElement('select');
        el.dataset.cast = 'bool';
        [['', '默认' + (field.default ? ' (true)' : ' (false)')], ['true', 'true'], ['false', 'false']]
            .forEach(([v, t]) => {
                const o = document.createElement('option');
                o.value = v; o.textContent = t;
                el.appendChild(o);
            });
    } else if (field.kind === 'int' || field.kind === 'float') {
        el = document.createElement('input');
        el.type = 'number';
        el.step = field.kind === 'int' ? '1' : 'any';
        el.placeholder = placeholderOf(field);
        el.dataset.cast = field.kind;
    } else if (field.kind === 'json') {
        el = document.createElement('input');
        el.type = 'text';
        el.className = 'json-input';
        el.placeholder = placeholderOf(field);
        el.dataset.cast = 'json';
    } else if (field.kind === 'list') {
        el = document.createElement('input');
        el.type = 'text';
        el.placeholder = placeholderOf(field) + '（逗号分隔）';
        el.dataset.cast = 'list';
    } else {
        el = document.createElement('input');
        el.type = 'text';
        el.placeholder = placeholderOf(field);
        el.dataset.cast = 'text';
    }
    el.dataset.name = field.name;
    el.spellcheck = false;
    wrap.appendChild(el);
    el.addEventListener('input', onCtrlChange);
    el.addEventListener('change', onCtrlChange);
    return { wrap, el };
}

function render() {
    GROUPS.innerHTML = '';
    const groups = (schema.groups || []).slice();
    groups.forEach((g) => {
        const fields = (schema.fields || []).filter(
            (f) => f.group === g.key && !f.managed && !managedFields.includes(f.name));
        if (!fields.length) return;

        const details = document.createElement('details');
        details.className = 'sect';
        details.dataset.group = g.key;
        details.open = ['connection', 'load', 'response'].includes(g.key);

        const summary = document.createElement('summary');
        const label = document.createElement('span');
        label.textContent = g.label;
        const count = document.createElement('span');
        count.className = 'badge-count muted';
        count.textContent = fields.length;
        summary.append(label, count);

        const body = document.createElement('div');
        body.className = 'sect-body';

        fields.forEach((f) => {
            meta[f.name] = f;
            const row = document.createElement('div');
            row.className = 'adv-field';
            row.dataset.name = f.name;
            const lab = document.createElement('div');
            lab.className = 'adv-label';
            const flag = document.createElement('span');
            flag.className = 'flag';
            flag.textContent = f.flag || ('--' + f.name.replace(/_/g, '-'));
            flag.title = (f.flag || '') + (f.help ? '\n' + f.help : '');
            if (f.required) {
                const req = document.createElement('span');
                req.className = 'req';
                req.textContent = '*';
                flag.appendChild(req);
            }
            lab.appendChild(flag);
            if (f.help) lab.title = f.help;

            const { wrap, el } = buildCtrl(f);
            if (f.help) el.title = f.help;
            row.append(lab, wrap);
            body.appendChild(row);
            ctrls[f.name] = { el, field: f, row, group: g.key };
        });

        details.append(summary, body);
        GROUPS.appendChild(details);
    });

    SCHEMA_DESC.textContent = 'evalscope perf · ' + (schema.fields || []).length + ' 个可配置参数'
        + (schema.version ? ' (v' + schema.version + ')' : '');

    MANAGED_NOTE.innerHTML = '以下参数由上方表单统一管理，不在此处重复配置：'
        + managedFields.map((n) => '<code>--' + n.replace(/_/g, '-') + '</code>').join(' ')
        + '（advanced 面板其余留空项即使用 evalscope 默认值）';

    refreshSetState();
}

/* ------------------------------------------------ 交互 */
function rawValue(name) {
    const c = ctrls[name];
    if (!c) return undefined;
    const v = c.el.value;
    if (v === '' || v === null || v === undefined) return undefined;
    const cast = c.el.dataset.cast;
    if (cast === 'bool') return v === 'true';
    if (cast === 'int') {
        const n = parseInt(v, 10);
        return isNaN(n) ? undefined : n;
    }
    if (cast === 'float') {
        const n = parseFloat(v);
        return isNaN(n) ? undefined : n;
    }
    if (cast === 'list') {
        const items = v.split(',').map((s) => s.trim()).filter((s) => s !== '');
        const itemKind = c.field.item_kind || 'text';
        return items.map((s) => {
            if (itemKind === 'int') { const n = parseInt(s, 10); return isNaN(n) ? s : n; }
            if (itemKind === 'float') { const n = parseFloat(s); return isNaN(n) ? s : n; }
            if (itemKind === 'bool') return s === 'true';
            return s;
        });
    }
    if (cast === 'json') {
        try { return JSON.parse(v); } catch (_) { return '__INVALID__'; }
    }
    return v;
}

function isSet(name) {
    const c = ctrls[name];
    if (!c) return false;
    const v = c.el.value;
    if (v === '' || v === null || v === undefined) return false;
    const f = c.field;
    const d = f.default;
    if (d === null || d === undefined) return true;
    if (typeof d === 'object') {
        try { return JSON.stringify(JSON.parse(v)) !== JSON.stringify(d); } catch (_) { return true; }
    }
    if (typeof d === 'number') {
        const n = parseFloat(v);
        return isNaN(n) ? true : n !== d;
    }
    if (typeof d === 'boolean') return (v === 'true') !== d;
    return String(v) !== String(d);
}

function onCtrlChange(e) {
    const el = e.target;
    const name = el.dataset.name;
    if (el.dataset.cast === 'json') {
        try { JSON.parse(el.value); el.classList.remove('invalid'); } catch (_) {
            el.classList.toggle('invalid', el.value.trim() !== '');
        }
    }
    const row = ctrls[name] && ctrls[name].row;
    if (row) row.classList.toggle('is-set', isSet(name));
    refreshSetState();
    applyFilter();
    if (core) core.scheduleSave();
}

function refreshSetState() {
    let n = 0;
    Object.keys(ctrls).forEach((name) => {
        const set = isSet(name);
        if (set) n++;
        const row = ctrls[name].row;
        row.classList.toggle('is-set', set);
        const groupEl = row.closest('details.sect');
        if (groupEl) {
            const total = groupEl.querySelectorAll('.adv-field').length;
            const setInGroup = groupEl.querySelectorAll('.adv-field.is-set').length;
            const badge = groupEl.querySelector('summary .badge-count');
            if (badge) {
                badge.textContent = setInGroup ? setInGroup + '/' + total : total;
                badge.classList.toggle('muted', setInGroup === 0);
            }
        }
    });
    SET_COUNT.textContent = n;
    SET_COUNT.classList.toggle('muted', n === 0);
}

function applyFilter() {
    const onlySet = ONLY_SET.checked;
    GROUPS.querySelectorAll('details.sect').forEach((d) => {
        let visible = 0;
        d.querySelectorAll('.adv-field').forEach((row) => {
            const show = !onlySet || row.classList.contains('is-set');
            row.style.display = show ? '' : 'none';
            if (show) visible++;
        });
        d.style.display = visible ? '' : 'none';
        if (onlySet) d.open = visible > 0;
    });
    const any = [...GROUPS.querySelectorAll('details.sect')].some((d) => d.style.display !== 'none');
    let empty = GROUPS.querySelector('.adv-empty');
    if (!any) {
        if (!empty) {
            empty = document.createElement('div');
            empty.className = 'adv-empty';
            empty.textContent = '当前没有修改过的高级参数';
            GROUPS.appendChild(empty);
        }
    } else if (empty) {
        empty.remove();
    }
}

/* ------------------------------------------------ 取值 / 设值 */
function getValues() {
    const out = {};
    Object.keys(ctrls).forEach((name) => {
        if (!isSet(name)) return;
        const v = rawValue(name);
        if (v === undefined || v === '__INVALID__') return;
        out[name] = v;
    });
    return out;
}

function setValue(name, value) {
    const c = ctrls[name];
    if (!c) return;
    const cast = c.el.dataset.cast;
    if (value === null || value === undefined) c.el.value = '';
    else if (cast === 'bool') c.el.value = value ? 'true' : 'false';
    else if (cast === 'list') {
        c.el.value = Array.isArray(value) ? value.join(',') : String(value);
    } else if (cast === 'json') {
        c.el.value = typeof value === 'string' ? value : JSON.stringify(value);
    } else if (cast === 'text') {
        c.el.value = Array.isArray(value) ? value.join(',') : String(value);
    } else {
        c.el.value = String(value);
    }
    c.row.classList.toggle('is-set', isSet(name));
}

function setValues(obj, opts) {
    opts = opts || {};
    if (opts.replace) {
        Object.keys(ctrls).forEach((name) => { ctrls[name].el.value = ''; ctrls[name].el.classList.remove('invalid'); });
    }
    Object.keys(obj || {}).forEach((k) => setValue(k, obj[k]));
    refreshSetState();
    applyFilter();
}

function reset() {
    Object.keys(ctrls).forEach((name) => {
        ctrls[name].el.value = '';
        ctrls[name].el.classList.remove('invalid');
        ctrls[name].row.classList.remove('is-set');
    });
    refreshSetState();
    applyFilter();
}

function validate() {
    for (const name of Object.keys(ctrls)) {
        const c = ctrls[name];
        if (c.el.dataset.cast !== 'json') continue;
        if (c.el.value.trim() === '') continue;
        try { JSON.parse(c.el.value); } catch (_) {
            c.el.classList.add('invalid');
            c.el.focus();
            return '参数 ' + (c.field.flag || name) + ' 需要合法 JSON';
        }
        c.el.classList.remove('invalid');
    }
    return '';
}

/* ------------------------------------------------ 命令解析 / 生成 */
function q(s) {
    const v = String(s);
    return /^[\w.\-/:@]+$/.test(v) ? v : '"' + v.replace(/"/g, '\\"') + '"';
}

function buildCommand() {
    if (!core) return '';
    const cfg = core.collectConfig();
    const parts = ['evalscope perf'];
    const add = (flag, val) => {
        if (val === undefined || val === null || val === '') return;
        if (Array.isArray(val)) {
            if (!val.length) return;
            parts.push(flag, val.map(q).join(' '));
            return;
        }
        parts.push(flag, q(val));
    };

    add('--model', cfg.model_name);
    add('--url', cfg.api_url);
    if (cfg.openai_api_keys.length) add('--api-key', cfg.openai_api_keys.join(','));

    const tt = cfg.task_types || [];
    if (tt.includes('openclaw')) {
        add('--dataset', cfg.openclaw_dataset_name);
        add('--dataset-path', cfg.openclaw_dataset_path);
    } else if (tt.includes('manual')) {
        add('--dataset', 'line_by_line');
        add('--dataset-path', '<手动数据集落盘文件.jsonl>');
    } else {
        add('--dataset', 'random');
        add('--tokenizer-path', cfg.tokenizer_path);
        const inputs = cfg.input_output_groups.map((g) => g.input);
        if (inputs.length) {
            add('--min-prompt-length', Math.min(...inputs));
            add('--max-prompt-length', Math.max(...inputs));
        }
    }

    if (cfg.concurrency_request_groups.length) {
        add('--parallel', cfg.concurrency_request_groups.map((g) => g.parallel));
        add('--number', cfg.concurrency_request_groups.map((g) => g.number));
    }
    const outputs = cfg.input_output_groups.map((g) => g.output);
    if (outputs.length) add('--max-tokens', outputs.length === 1 ? outputs[0] : outputs);

    add('--stream', cfg.stream ? 'true' : 'false');
    add('--log-every-n-query', cfg.log_every_n_query);
    add('--prefix-length', cfg.prefix_length);

    const adv = cfg.evalscope_args || {};
    Object.keys(adv).sort().forEach((k) => {
        const v = adv[k];
        if (v === undefined || v === null || v === '') return;
        const flag = '--' + k.replace(/_/g, '-');
        if (typeof v === 'object' && !Array.isArray(v)) {
            parts.push(flag, "'" + JSON.stringify(v).replace(/'/g, "'\\''") + "'");
        } else if (Array.isArray(v)) {
            parts.push(flag, v.map(q).join(' '));
        } else if (typeof v === 'boolean') {
            parts.push(flag, v ? 'true' : 'false');
        } else {
            parts.push(flag, q(v));
        }
    });
    return parts.join(' ');
}

function showMsg(html, type) {
    const box = $('cmd-msg');
    box.className = 'cmd-msg show ' + (type || 'ok');
    box.innerHTML = html;
}

function hideMsg() {
    $('cmd-msg').className = 'cmd-msg';
    $('cmd-msg').innerHTML = '';
}

async function parseCommand() {
    const cmd = $('cmd-input').value.trim();
    if (!cmd) { showMsg('请先粘贴一条 evalscope perf 命令', 'warn'); return; }
    const btn = $('btn-cmd-parse');
    btn.disabled = true;
    btn.textContent = '解析中…';
    try {
        const res = await fetch('/evalscope/parse-command', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ command: cmd }),
        });
        const data = await res.json();
        if (!data.ok) {
            showMsg('<strong>解析失败</strong><br>' + escapeHtml(data.error || '未知错误'), 'err');
            return;
        }
        core.applyConfig(data.config);
        const warnings = data.warnings || [];
        const unknown = data.unknown_flags || [];
        let html = '<strong>已回填表单</strong>：'
            + '并行组 ' + (data.config.concurrency_request_groups || []).length + ' 个 · '
            + 'IO 组 ' + (data.config.input_output_groups || []).length + ' 个 · '
            + '高级参数 ' + Object.keys(data.config.evalscope_args || {}).length + ' 项'
            + '<br>重复次数 ' + (core.collectConfig().test_repeats) + ' 次、测试间隔沿用「执行参数」设置';
        if (warnings.length) {
            html += '<ul>' + warnings.map((w) => '<li>' + escapeHtml(w) + '</li>').join('') + '</ul>';
        }
        if (unknown.length) {
            html += '<br>未识别参数：<code>' + unknown.map(escapeHtml).join(' ') + '</code>';
        }
        showMsg(html, warnings.length ? 'warn' : 'ok');
        core.scheduleSave();
    } catch (e) {
        showMsg('<strong>请求失败</strong><br>' + escapeHtml(e.message), 'err');
    } finally {
        btn.disabled = false;
        btn.textContent = '解析并填充';
    }
}

function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

const SAMPLE = 'evalscope perf --model qwen3-8b '
    + '--url http://127.0.0.1:8000/v1/chat/completions --api-key sk-xxxxxxxx '
    + '--dataset random --tokenizer-path D:/models/Qwen3-8B '
    + '--parallel 16 32 64 --number 80 160 320 '
    + '--max-tokens 500 --min-prompt-length 32768 --max-prompt-length 32768 '
    + '--stream true --temperature 0.6 --top-p 0.95 --seed 1234 --warmup-num 5';

/* ------------------------------------------------ 初始化 */
function bind() {
    $('btn-cmd-parse').addEventListener('click', parseCommand);
    $('btn-cmd-clear').addEventListener('click', () => {
        $('cmd-input').value = '';
        hideMsg();
    });
    $('btn-cmd-sample').addEventListener('click', () => {
        $('cmd-input').value = SAMPLE;
        showMsg('已填入示例命令，点击「解析并填充」写入表单', 'ok');
    });
    $('btn-cmd-build').addEventListener('click', () => {
        const cmd = buildCommand();
        $('cmd-input').value = cmd;
        showMsg('已根据当前表单生成等价命令，可直接复制到终端使用', 'ok');
        if (core) core.scheduleSave();
    });
    ONLY_SET.addEventListener('change', applyFilter);
    $('btn-adv-reset').addEventListener('click', () => {
        reset();
        core.toast('高级参数已重置为默认');
        core.scheduleSave();
    });
}

let resolveReady;
const ready = new Promise((r) => { resolveReady = r; });

fetch('/evalscope/schema')
    .then((r) => r.json())
    .then((data) => {
        if (!data.ok) throw new Error(data.error || 'schema 加载失败');
        schema = data;
        managedFields = data.managed_fields || [];
        render();
        resolveReady();
    })
    .catch((e) => {
        SCHEMA_DESC.textContent = '加载失败：' + e.message;
        GROUPS.innerHTML = '<div class="adv-empty">无法加载 evalscope 参数 schema：' + escapeHtml(e.message) + '</div>';
        resolveReady();
    });

document.addEventListener('DOMContentLoaded', () => {
    bind();
    ready.then(() => applyFilter());
});

window.AdvPanel = {
    ready,
    init: (c) => { core = c; },
    getValues, setValues, reset, validate, buildCommand,
};
})();
