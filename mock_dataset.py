#!/usr/bin/env python3
"""把真实抓取的 mock 请求体转换成 evalscope 压测数据集。

源目录（用 ``--src`` 指定，或设置环境变量 ``MOCK_SRC_DIR`` 作为默认值）::

    v1_messages_request.json     anthropic 形态，735 条 messages（368 user / 367 assistant）
    openai_chat_request.json     同一会话的 openai 形态（tool_calls / tool_call_id）
    *_single_request.json        整段会话压成一条 347K 字符 user 文本

源会话内容：真人只问了约 22 次，模型自己做了 346 次工具调用（SessionSearch /
Skill / playwright 等），因此 735 条消息里绝大多数是 tool_use ↔ tool_result 往返。

产出（默认 ``datasets/``）::

    mock_agent_trace_raw.jsonl    N 行，**保真**快照：tool_use / tool_result 原样保留
                                  → dataset=mock_messages（真正的「第 N 次追问的完整请求体」）
    mock_agent_trace_raw_tools300.jsonl  只保留 tool_use ≥ 300 的快照（直接压「300+ 次工具调用」）
    mock_agent_trace.jsonl        N 行，每行是该时刻的「完整历史快照」（工具块已渲染成文本）
                                  → dataset=mock_messages（文本形态，token 更省）
    mock_agent_trace_long.jsonl   按上下文长度降序取前 20 行（小 number 即可压长上下文）
    mock_agent_multi_turn.jsonl   1 行，22 轮真人提问（工具结果内联进本轮 user 文本）
                                  → dataset=custom_multi_turn
    mock_agent_prompt.jsonl       1 行，347K 字符单条 prompt
                                  → dataset=custom（超长单轮）
    manifest.json                 元信息，供前端「内置数据集」下拉读取

用法::

    python mock_dataset.py
    python mock_dataset.py --src "C:\\path\\to\\mock" --out datasets
"""

from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any, Dict, Iterable, List

# 源目录不入库：用 --src 指定，或设置环境变量 MOCK_SRC_DIR 作为默认值
DEFAULT_SRC = os.environ.get('MOCK_SRC_DIR', '')
DEFAULT_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'datasets')

ANTHROPIC_FILE = 'v1_messages_request.json'
OPENAI_FILE = 'openai_chat_request.json'
SINGLE_FILES = ('anthropic_single_request.json', 'openai_single_request.json')

#: 内联工具结果时，单条结果的字符上限（None = 不截断）
TOOL_RESULT_LIMIT = None


# ---------------------------------------------------------------- 文本提取
def _blocks(message: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把消息 content 归一化成 block 列表。"""
    content = message.get('content')
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    if isinstance(content, str):
        return [{'type': 'text', 'text': content}]
    return []


def _text_of(value: Any) -> str:
    """tool_result.content 可能是 str，也可能是 [{type:text,text}, ...]。"""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for b in value:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                if b.get('type') == 'text' and b.get('text'):
                    parts.append(b['text'])
                elif b.get('type') == 'image':
                    parts.append('[image]')
        return '\n'.join(parts)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return '' if value is None else str(value)


def _clip(text: str) -> str:
    if TOOL_RESULT_LIMIT and len(text) > TOOL_RESULT_LIMIT:
        return text[:TOOL_RESULT_LIMIT] + f'…（截断，原 {len(text)} 字符）'
    return text


def _tool_use_text(block: Dict[str, Any]) -> str:
    name = block.get('name') or 'tool'
    args = block.get('input')
    try:
        args_text = json.dumps(args, ensure_ascii=False)
    except (TypeError, ValueError):
        args_text = str(args)
    return f'[调用工具] {name}({args_text})'


def _tool_result_text(block: Dict[str, Any]) -> str:
    return '[工具返回] ' + _clip(_text_of(block.get('content')))


def _thinking_text(block: Dict[str, Any]) -> str:
    return '[思考] ' + (block.get('thinking') or '')


# ---------------------------------------------------------------- 源解析
def _load_source(src: str) -> tuple[str, List[Dict[str, Any]]]:
    if not src:
        raise FileNotFoundError('未指定源目录：请用 --src 传入，或设置环境变量 MOCK_SRC_DIR')
    for name in (ANTHROPIC_FILE, OPENAI_FILE):
        path = os.path.join(src, name)
        if os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            messages = data.get('messages') or []
            if messages:
                return name, messages
    raise FileNotFoundError(f'{src} 下找不到 {ANTHROPIC_FILE} / {OPENAI_FILE}')


def _normalize_openai(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """openai 形态 → 统一的文本块形态（tool_calls → [调用工具]，tool → tool_result）。"""
    out: List[Dict[str, Any]] = []
    for m in messages:
        role = m.get('role')
        content = m.get('content')
        if role == 'assistant':
            blocks = []
            if m.get('reasoning_content'):
                blocks.append({'type': 'thinking', 'thinking': m['reasoning_content']})
            if content:
                blocks.append({'type': 'text', 'text': content if isinstance(content, str) else _text_of(content)})
            for call in (m.get('tool_calls') or []):
                fn = call.get('function') or {}
                args = fn.get('arguments')
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        pass
                blocks.append({'type': 'tool_use', 'name': fn.get('name'), 'input': args, 'id': call.get('id')})
            out.append({'role': 'assistant', 'content': blocks})
        elif role == 'tool':
            out.append({
                'role': 'user',
                'content': [{'type': 'tool_result', 'content': content, 'tool_use_id': m.get('tool_call_id')}],
            })
        else:
            out.append({'role': role, 'content': content})
    return out


# ---------------------------------------------------------------- 转换
def _user_side_text(message: Dict[str, Any]) -> tuple[str, bool, bool]:
    """返回 (内联文本, 是否含真实提问, 是否含工具结果)。"""
    parts: List[str] = []
    has_human = False
    has_tool = False
    for block in _blocks(message):
        btype = block.get('type')
        if btype == 'text':
            text = (block.get('text') or '').strip()
            if text:
                parts.append(text)
                has_human = True
        elif btype == 'tool_result':
            parts.append(_tool_result_text(block))
            has_tool = True
    return '\n'.join(parts), has_human, has_tool


def _assistant_text(message: Dict[str, Any]) -> str:
    parts: List[str] = []
    for block in _blocks(message):
        btype = block.get('type')
        if btype == 'thinking':
            thinking = (block.get('thinking') or '').strip()
            if thinking:
                parts.append(_thinking_text(block))
        elif btype == 'text':
            text = (block.get('text') or '').strip()
            if text:
                parts.append(text)
        elif btype == 'tool_use':
            parts.append(_tool_use_text(block))
    return '\n'.join(parts)


def build_trace(messages: List[Dict[str, Any]]) -> List[List[Dict[str, str]]]:
    """每个 user 侧输入到达时切一个快照，等价于真实请求序列。"""
    history: List[Dict[str, str]] = []
    snapshots: List[List[Dict[str, str]]] = []
    for message in messages:
        role = message.get('role')
        if role == 'user':
            text, _, _ = _user_side_text(message)
            if not text:
                continue
            history = history + [{'role': 'user', 'content': text}]
            snapshots.append([dict(m) for m in history])
        else:
            text = _assistant_text(message)
            if not text:
                continue
            history = history + [{'role': 'assistant', 'content': text}]
    return snapshots


#: 真实 Anthropic 要求 ``thinking`` 块带 ``signature``，抓包里的 thinking 全部没有
#: signature，原样回传会被拒（``thinking blocks must include signature``）。压测也不
#: 需要重放思考内容，所以保真快照一律剔除 thinking，只留 text / tool_use / tool_result。
_DROP_BLOCK_TYPES = {'thinking', 'redacted_thinking'}


def _raw_blocks(message: Dict[str, Any]) -> Any:
    """保留原始 block 结构，仅剔除不可回传的 thinking 块。"""
    content = message.get('content')
    if isinstance(content, list):
        blocks = [
            b for b in content
            if isinstance(b, dict) and b.get('type') not in _DROP_BLOCK_TYPES
        ]
        return blocks or None
    if isinstance(content, str):
        return content if content.strip() else None
    return None


def _count_tool_use(history: List[Dict[str, Any]]) -> int:
    total = 0
    for m in history:
        c = m.get('content')
        if isinstance(c, list):
            total += sum(1 for b in c if isinstance(b, dict) and b.get('type') == 'tool_use')
    return total


def build_trace_raw(messages: List[Dict[str, Any]]) -> List[tuple]:
    """保真快照：tool_use / tool_result 原样保留，返回 ``[(messages, tool_use 数)]``。

    与 :func:`build_trace` 的区别是**不做文本渲染**，因此 tool_use 的 ``id`` 与
    tool_result 的 ``tool_use_id`` 仍一一配对，可以安全发给 Anthropic /v1/messages。
    每个 user 侧输入到达时切一份完整历史，等价于「用户第 N 次追问时实际发出的请求体」。
    """
    history: List[Dict[str, Any]] = []
    snapshots: List[tuple] = []
    for message in messages:
        role = message.get('role')
        if role not in ('user', 'assistant'):
            continue
        content = _raw_blocks(message)
        if not content:
            # 剔除 thinking 后变空的消息：直接丢掉，不影响 tool 配对
            # （tool_use 一定和 text 或独立 tool_use 块在一起，不会只存在于 thinking 里）
            continue
        history = history + [{'role': role, 'content': content}]
        if role == 'user':
            snapshots.append(([dict(m) for m in history], _count_tool_use(history)))
    return snapshots


def build_multi_turn(messages: List[Dict[str, Any]]) -> List[List[Dict[str, str]]]:
    """按真人提问切轮：每轮内联该轮所有工具往返，assistant 仅作轮次分隔。"""
    turns: List[List[Dict[str, str]]] = []
    pending: List[str] = []
    for message in messages:
        role = message.get('role')
        if role == 'user':
            text, has_human, _ = _user_side_text(message)
            if not text:
                continue
            if has_human and pending:
                turns.append(pending)
                pending = []
            pending.append(text)
        # assistant 内容丢弃：多轮压测要求模型重新生成
    if pending:
        turns.append(pending)

    rows: List[List[Dict[str, str]]] = []
    conversation: List[Dict[str, str]] = []
    for turn_texts in turns:
        conversation.append({'role': 'user', 'content': '\n\n'.join(turn_texts)})
        conversation.append({'role': 'assistant', 'content': ''})  # 轮次边界，插件会丢弃
    if conversation and conversation[-1]['role'] == 'assistant':
        conversation.pop()
    if conversation:
        rows.append(conversation)
    return rows


def _write_jsonl(path: str, rows: Iterable[Any]) -> int:
    n = 0
    with open(path, 'w', encoding='utf-8') as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
            n += 1
    return n


def _flat_text(value: Any) -> str:
    """把 str / block list / block dict 拍平成纯文本，用于 token 估算。"""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return '\n'.join(_flat_text(v) for v in value)
    if isinstance(value, dict):
        parts = []
        for key in ('text', 'thinking', 'content', 'input'):
            if key in value:
                v = value[key]
                parts.append(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))
        return '\n'.join(parts)
    return ''


def _row_chars(row: List[Dict[str, Any]]) -> int:
    """单份快照的字符数（content 可能是 str，也可能是 block list）。"""
    total = 0
    for m in row:
        content = m.get('content')
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            total += len(json.dumps(content, ensure_ascii=False))
    return total


def _token_estimate(rows: List[Any], tokenizer_dir: str) -> dict:
    info: Dict[str, Any] = {}
    try:
        from transformers import AutoTokenizer
        tk = AutoTokenizer.from_pretrained(tokenizer_dir, trust_remote_code=True)
    except Exception as exc:  # noqa: BLE001
        info['tokenizer_error'] = str(exc)[:120]
        return info
    counts = []
    sample = rows[:3] + rows[-2:] if len(rows) > 5 else rows
    for row in sample:
        text = '\n'.join(_flat_text(m.get('content')) for m in row) if isinstance(row, list) else str(row)
        try:
            counts.append(len(tk.encode(text)))
        except Exception:  # noqa: BLE001
            pass
    if counts:
        info['tokens_sample'] = counts
    return info


# ---------------------------------------------------------------- main
def main() -> None:
    parser = argparse.ArgumentParser(description='把真实 mock 请求体转换成 evalscope 压测数据集')
    parser.add_argument('--src', default=DEFAULT_SRC,
                        help='mock 请求体所在目录（默认取环境变量 MOCK_SRC_DIR）')
    parser.add_argument('--out', default=DEFAULT_OUT, help='输出目录')
    parser.add_argument('--tokenizer', default='', help='可选：tokenizer 目录，用于估算 token 数')
    args = parser.parse_args()

    src_name, raw_messages = _load_source(args.src)
    if src_name == OPENAI_FILE:
        raw_messages = _normalize_openai(raw_messages)

    os.makedirs(args.out, exist_ok=True)

    trace = build_trace(raw_messages)
    multi = build_multi_turn(raw_messages)
    raw_pairs = build_trace_raw(raw_messages)

    trace_path = os.path.join(args.out, 'mock_agent_trace.jsonl')
    multi_path = os.path.join(args.out, 'mock_agent_multi_turn.jsonl')
    raw_path = os.path.join(args.out, 'mock_agent_trace_raw.jsonl')
    raw300_path = os.path.join(args.out, 'mock_agent_trace_raw_tools300.jsonl')
    _write_jsonl(trace_path, trace)
    _write_jsonl(multi_path, multi)
    _write_jsonl(raw_path, [snap for snap, _ in raw_pairs])
    raw300 = [snap for snap, n_tool in raw_pairs if n_tool >= 300]
    _write_jsonl(raw300_path, raw300)

    # 长上下文版本：按单请求字符数降序取前 20 个快照，配合小 number 直接压最长上下文
    trace_long = sorted(trace, key=_row_chars, reverse=True)[:20]
    long_path = os.path.join(args.out, 'mock_agent_trace_long.jsonl')
    _write_jsonl(long_path, trace_long)

    entries = [
        {
            'file': os.path.basename(raw_path),
            'name': 'mock_messages',
            'kind': 'trace_raw',
            'label': '真实请求体重放（tool_use/tool_result 原样保留，按工具调用次数可直接选中 300+ 那份）',
            'rows': len(raw_pairs),
            'turns': 1,
            'multi_turn': False,
            'chars': sum(_row_chars(snap) for snap, _ in raw_pairs),
            'max_chars': max((_row_chars(snap) for snap, _ in raw_pairs), default=0),
            'tool_results': max((n for _, n in raw_pairs), default=0),
            'tool_uses': max((n for _, n in raw_pairs), default=0),
        },
        {
            'file': os.path.basename(raw300_path),
            'name': 'mock_messages',
            'kind': 'trace_raw_tools300',
            'label': '工具调用 ≥300 次的完整请求体（单次请求即含数百次 tool_use/tool_result 往返）',
            'rows': len(raw300),
            'turns': 1,
            'multi_turn': False,
            'chars': sum(_row_chars(snap) for snap in raw300),
            'max_chars': max((_row_chars(snap) for snap in raw300), default=0),
            'tool_uses': max((n for _, n in raw_pairs if n >= 300), default=0),
        },
        {
            'file': os.path.basename(trace_path),
            'name': 'mock_messages',
            'kind': 'trace',
            'label': '真实 agent 轨迹重放（每行一份历史快照）',
            'rows': len(trace),
            'turns': 1,
            'multi_turn': False,
            'chars': sum(len(m['content']) for row in trace for m in row),
            'max_chars': max((sum(len(m['content']) for m in row) for row in trace), default=0),
            'tool_results': max((sum(1 for m in row if m['content'].startswith('[工具返回]')) for row in trace),
                                default=0),
        },
        {
            'file': os.path.basename(long_path),
            'name': 'mock_messages',
            'kind': 'trace_long',
            'label': '长上下文子集（按长度降序前 20 条，小 number 直接压最长上下文）',
            'rows': len(trace_long),
            'turns': 1,
            'multi_turn': False,
            'chars': sum(len(m['content']) for row in trace_long for m in row),
            'max_chars': max((_row_chars(row) for row in trace_long), default=0),
            'tool_results': max((sum(1 for m in row if m['content'].startswith('[工具返回]')) for row in trace_long),
                                default=0),
        },
        {
            'file': os.path.basename(multi_path),
            'name': 'custom_multi_turn',
            'kind': 'multi_turn',
            'label': '真实 agent 多轮对话（工具结果内联，模型重新作答）',
            'rows': len(multi),
            'turns': max((sum(1 for m in row if m['role'] == 'user') for row in multi), default=0),
            'multi_turn': True,
            'chars': sum(len(m['content']) for row in multi for m in row),
            'max_chars': max((sum(len(m['content']) for m in row) for row in multi), default=0),
        },
    ]

    # 超长单条 prompt（原始 347K 字符）
    for name in SINGLE_FILES:
        path = os.path.join(args.src, name)
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        messages = data.get('messages') or []
        text = ''.join(_text_of(m.get('content')) for m in messages)
        if not text:
            continue
        out_path = os.path.join(args.out, 'mock_agent_prompt.jsonl')
        _write_jsonl(out_path, [text])
        entries.append({
            'file': os.path.basename(out_path),
            'name': 'custom',
            'kind': 'single_prompt',
            'label': '超长单条 prompt（整段会话压成一个 user 文本）',
            'rows': 1,
            'turns': 1,
            'multi_turn': False,
            'tool_results': 0,
            'chars': len(text),
            'max_chars': len(text),
        })
        break

    if args.tokenizer:
        row_map = {
            os.path.basename(trace_path): trace,
            os.path.basename(long_path): trace_long,
            os.path.basename(multi_path): multi,
            os.path.basename(raw_path): [snap for snap, _ in raw_pairs],
            os.path.basename(raw300_path): raw300,
        }
        for entry in entries:
            rows = row_map.get(entry['file'])
            if rows:
                entry.update(_token_estimate(rows, args.tokenizer))

    manifest = {
        'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'source': args.src,
        'source_file': src_name,
        'source_messages': len(raw_messages),
        'datasets': entries,
    }
    with open(os.path.join(args.out, 'manifest.json'), 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f'源文件: {src_name}（{len(raw_messages)} 条 messages）')
    for entry in entries:
        print(f"  {entry['file']:<28} rows={entry['rows']:<5} turns={entry['turns']:<4} "
              f"chars={entry['chars']:,} max/req={entry['max_chars']:,}")


if __name__ == '__main__':
    main()
