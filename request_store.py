"""读取 evalscope perf 落盘的 ``benchmark_data.db``（SQLite），供前端查看单次请求详情。

evalscope 的表结构（``evalscope/perf/utils/db_util.py``）::

    result(request TEXT, start_time REAL, inter_token_latencies TEXT, success INTEGER,
           response_messages TEXT, completed_time REAL, latency REAL,
           first_chunk_latency REAL, prompt_tokens INTEGER, completion_tokens INTEGER,
           max_gpu_memory_cost REAL, time_per_output_token REAL)

其中 ``response_messages`` 是 ``base64(pickle(list))``，必须在本模块解码；
``status_code`` / ``error`` / ``trace_id`` / ``turns`` / ``generated_text`` /
``response_headers`` 是 ``runner._patch_evalscope_db()`` 运行时补出来的列
（老库里可能不存在，本模块做了兼容）。其中 ``response_headers`` 由
``perf_anthropic`` 插件把 HTTP 响应头 json 序列化后写入（仅 anthropic 协议有值）。
"""

from __future__ import annotations

import base64
import io
import json
import pickle
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

DB_NAME = "benchmark_data.db"

#: 单次详情返回的最大字符数，避免多轮长上下文把浏览器拖死
MAX_DETAIL_CHARS = 1_200_000

#: 数据库里可能存在的扩展列（由 runner patch 写入）
EXTRA_COLUMNS = (
    "status_code",
    "error",
    "trace_id",
    "turns",
    "generated_text",
)

_TEXT_KEYS = ("content", "text")
_REASON_KEYS = ("reasoning_content", "thinking")


class _SafeUnpickler(pickle.Unpickler):
    """只允许基础类型，拒绝反序列化任意类（db 内容不可信时的兜底）。"""

    def find_class(self, module: str, name: str):  # noqa: ANN001
        raise pickle.UnpicklingError(f"拒绝反序列化 {module}.{name}")


# --------------------------------------------------------------------------- 发现
def list_databases(history_root: Path) -> List[Dict[str, Any]]:
    """列出所有 sub-test 的 db 文件（按修改时间倒序）。"""
    root = Path(history_root)
    items: List[Dict[str, Any]] = []
    if not root.exists():
        return items
    for db in root.glob(f"*/outputs/**/{DB_NAME}"):
        try:
            stat = db.stat()
        except OSError:
            continue
        rel = db.relative_to(root).as_posix()
        run_id = rel.split("/", 1)[0]
        label = rel.split("/outputs/", 1)[-1].rsplit("/", 1)[0] or "."
        total = ok = bad = 0
        con = None
        try:
            con = _connect(db)
            total, ok, bad = _count(con)
        except sqlite3.Error:
            total = ok = bad = 0
        finally:
            if con is not None:
                con.close()
        items.append({
            "id": rel,
            "run_id": run_id,
            "label": label,
            "path": str(db),
            "total": total,
            "success": ok,
            "failed": bad,
            "size": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        })
    items.sort(key=lambda x: (x["run_id"], x["label"]), reverse=True)
    return items


def resolve_db(history_root: Path, rel_or_abs: str) -> Optional[Path]:
    """把前端传来的 db 标识解析为路径，且必须位于 ``history_root`` 内。"""
    if not rel_or_abs:
        return None
    root = Path(history_root).resolve()
    candidate = Path(rel_or_abs)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        candidate = candidate.resolve()
    except OSError:
        return None
    if not candidate.is_file() or candidate.name != DB_NAME:
        return None
    if root not in candidate.parents:
        return None
    return candidate


# --------------------------------------------------------------------------- 基础
def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=5)


def _columns(con: sqlite3.Connection) -> set:
    try:
        return {row[1] for row in con.execute("PRAGMA table_info(result)")}
    except sqlite3.Error:
        return set()


def _count(con: sqlite3.Connection) -> tuple:
    total = con.execute("SELECT COUNT(*) FROM result").fetchone()[0]
    ok = con.execute("SELECT COUNT(*) FROM result WHERE success=1").fetchone()[0]
    return int(total), int(ok), int(total) - int(ok)


def _status_expr(cols: set) -> str:
    """没有 status_code 列的老库：用 success 推断 200/NULL。"""
    if "status_code" in cols:
        return "status_code"
    return "CASE WHEN success=1 THEN 200 ELSE NULL END"


def _where(status: str, keyword: str, cols: set) -> tuple:
    clauses: List[str] = []
    params: List[Any] = []
    status = (status or "").strip().lower()
    status_col = _status_expr(cols)

    if status in ("ok", "success", "2xx"):
        clauses.append("success=1")
    elif status in ("failed", "fail", "error"):
        clauses.append("success=0")
    elif status in ("4xx", "5xx"):
        clauses.append(f"{status_col} BETWEEN ? AND ?")
        base = 400 if status == "4xx" else 500
        params.extend([base, base + 99])
    elif status.isdigit():
        clauses.append(f"{status_col}=?")
        params.append(int(status))

    if keyword:
        like = f"%{keyword}%"
        searchable = ["request"]
        for col in ("error", "generated_text"):
            if col in cols:
                searchable.append(col)
        clauses.append("(" + " OR ".join(f"{c} LIKE ?" for c in searchable) + ")")
        params.extend([like] * len(searchable))

    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def status_counts(con: sqlite3.Connection) -> Dict[str, int]:
    cols = _columns(con)
    status_col = _status_expr(cols)
    rows = con.execute(
        f"SELECT {status_col} AS code, COUNT(*) FROM result GROUP BY code ORDER BY COUNT(*) DESC"
    ).fetchall()
    counts: Dict[str, int] = {}
    for code, n in rows:
        key = "unknown" if code is None else str(int(code))
        counts[key] = counts.get(key, 0) + int(n)
    return counts


# --------------------------------------------------------------------------- 查询
def query(
    path: Path,
    status: str = "",
    keyword: str = "",
    page: int = 1,
    size: int = 20,
) -> Dict[str, Any]:
    """分页查询请求记录（不含响应正文，点开详情再看）。"""
    page = max(int(page or 1), 1)
    size = min(max(int(size or 20), 1), 200)
    con = _connect(path)
    try:
        con.row_factory = sqlite3.Row
        cols = _columns(con)
        total, ok, bad = _count(con)
        base = _base_time(con)
        where, params = _where(status, keyword, cols)
        matched = con.execute(f"SELECT COUNT(*) FROM result{where}", params).fetchone()[0]
        rows = con.execute(
            f"""SELECT rowid AS rid, start_time, completed_time, latency, first_chunk_latency,
                       success, prompt_tokens, completion_tokens,
                       {_status_expr(cols)} AS status_code,
                       {'error' if 'error' in cols else 'NULL'} AS error,
                       {'trace_id' if 'trace_id' in cols else 'NULL'} AS trace_id,
                       {'turns' if 'turns' in cols else '0'} AS turns,
                       {'generated_text' if 'generated_text' in cols else 'NULL'} AS generated_text,
                       length(request) AS request_size,
                       length(response_messages) AS response_size
                FROM result{where}
                ORDER BY rowid DESC LIMIT ? OFFSET ?""",
            params + [size, (page - 1) * size],
        ).fetchall()
        counts = status_counts(con)
    finally:
        con.close()

    items = []
    for row in rows:
        d = dict(row)
        text = (d.pop("generated_text", None) or "").strip()
        err = d.get("error") or ""
        items.append({
            "id": d["rid"],
            "start_time": _rel_time(d.get("start_time"), base),
            "latency": _round(d.get("latency")),
            "ttft": _round(d.get("first_chunk_latency")),
            "success": bool(d.get("success")),
            "status_code": d.get("status_code"),
            "prompt_tokens": d.get("prompt_tokens"),
            "completion_tokens": d.get("completion_tokens"),
            "turns": d.get("turns") or 0,
            "trace_id": d.get("trace_id") or "",
            "request_size": d.get("request_size") or 0,
            "response_size": d.get("response_size") or 0,
            "preview": (text or err)[:180],
            "error": err[:300],
        })

    return {
        "total": total,
        "success": ok,
        "failed": bad,
        "matched": int(matched),
        "page": page,
        "size": size,
        "pages": max((int(matched) + size - 1) // size, 1),
        "status_counts": counts,
        "items": items,
    }


def detail(path: Path, rowid: int) -> Optional[Dict[str, Any]]:
    """取单条记录：请求体（JSON 文本）+ 解码后的响应内容。"""
    con = _connect(path)
    try:
        con.row_factory = sqlite3.Row
        cols = _columns(con)
        base = _base_time(con)
        row = con.execute(
            f"""SELECT rowid AS rid, request, response_messages, start_time, completed_time,
                       latency, first_chunk_latency, success, prompt_tokens, completion_tokens,
                       {_status_expr(cols)} AS status_code,
                       {'error' if 'error' in cols else 'NULL'} AS error,
                       {'trace_id' if 'trace_id' in cols else 'NULL'} AS trace_id,
                       {'turns' if 'turns' in cols else '0'} AS turns,
                       {'response_headers' if 'response_headers' in cols else 'NULL'} AS response_headers
                FROM result WHERE rowid=?""",
            (int(rowid),),
        ).fetchone()
    finally:
        con.close()
    if row is None:
        return None

    d = dict(row)
    request_text = d.get("request") or ""
    response = decode_response(d.get("response_messages"))

    req_parsed: Any = request_text
    try:
        req_parsed = json.loads(request_text)
    except (TypeError, ValueError):
        pass

    return {
        "id": d["rid"],
        "start_time": _rel_time(d.get("start_time"), base),
        "latency": _round(d.get("latency")),
        "ttft": _round(d.get("first_chunk_latency")),
        "success": bool(d.get("success")),
        "status_code": d.get("status_code"),
        "prompt_tokens": d.get("prompt_tokens"),
        "completion_tokens": d.get("completion_tokens"),
        "turns": d.get("turns") or 0,
        "trace_id": d.get("trace_id") or "",
        "error": (d.get("error") or "")[:10000],
        "request": _truncate_obj(req_parsed),
        "request_raw": _truncate_text(request_text),
        "response_headers": _headers_obj(d.get("response_headers")),
        "response": {
            "text": _truncate_text(response.get("text") or ""),
            "reasoning": _truncate_text(response.get("reasoning") or ""),
            "tool_calls": response.get("tool_calls") or [],
            "decoded": bool(response.get("decoded")),
            "events": _truncate_obj(response.get("events") or []),
        },
    }


# --------------------------------------------------------------------------- 解码
def _decode_messages(blob: Any) -> Any:
    if not blob:
        return None
    if isinstance(blob, (bytes, bytearray)):
        raw = bytes(blob)
    else:
        try:
            raw = base64.b64decode(blob)
        except Exception:  # noqa: BLE001
            return None
    try:
        return _SafeUnpickler(io.BytesIO(raw)).load()
    except Exception:  # noqa: BLE001
        return None


def decode_response(blob: Any) -> Dict[str, Any]:
    """把 ``response_messages`` 还原成文本、思考内容、tool_calls 与原始事件列表。"""
    messages = _decode_messages(blob)
    if messages is None:
        return {"text": "", "reasoning": "", "tool_calls": [], "events": [], "decoded": False}
    if not isinstance(messages, list):
        messages = [messages]

    text_parts: List[str] = []
    reason_parts: List[str] = []
    tool_calls: Dict[str, Dict[str, Any]] = {}
    events: List[Any] = []

    for item in messages:
        if isinstance(item, (str, int, float)):
            text_parts.append(str(item))
            continue
        if not isinstance(item, dict):
            continue
        events.append(item)
        _openai_parts(item, tool_calls, text_parts, reason_parts)
        _anthropic_parts(item, tool_calls, text_parts, reason_parts)

    return {
        "text": "".join(text_parts),
        "reasoning": "".join(reason_parts),
        "tool_calls": list(tool_calls.values()),
        "events": events,
        "decoded": True,
    }


def _openai_parts(item: Dict[str, Any], tool_calls: Dict[str, Dict[str, Any]],
                  out: List[str], reason: List[str]) -> None:
    for choice in item.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        holder = choice.get("delta") or choice.get("message") or {}
        if not isinstance(holder, dict):
            continue
        for key in _TEXT_KEYS:
            value = holder.get(key)
            if isinstance(value, str) and value:
                out.append(value)
        for key in _REASON_KEYS:
            value = holder.get(key)
            if isinstance(value, str) and value:
                reason.append(value)
        _merge_tool_calls(holder.get("tool_calls"), tool_calls)
        msg_calls = choice.get("message", {}).get("tool_calls") if isinstance(choice.get("message"), dict) else None
        _merge_tool_calls(msg_calls, tool_calls)


def _merge_tool_calls(calls: Any, bucket: Dict[str, Dict[str, Any]]) -> None:
    if not isinstance(calls, list):
        return
    for idx, call in enumerate(calls):
        if not isinstance(call, dict):
            continue
        fn = call.get("function") or {}
        key = str(call.get("index", call.get("id") or fn.get("name") or idx))
        slot = bucket.setdefault(key, {"id": call.get("id") or "", "name": "", "arguments": ""})
        if call.get("id"):
            slot["id"] = call["id"]
        if fn.get("name"):
            slot["name"] = fn["name"]
        args = fn.get("arguments")
        if isinstance(args, str):
            slot["arguments"] += args
        elif args is not None:
            slot["arguments"] = json.dumps(args, ensure_ascii=False)


def _anthropic_parts(item: Dict[str, Any], tool_calls: Dict[str, Dict[str, Any]],
                     out: List[str], reason: List[str]) -> None:
    etype = item.get("type")

    if etype == "content_block_start":
        block = item.get("content_block") or {}
        if not isinstance(block, dict):
            return
        if block.get("type") == "tool_use":
            # 用 index 对齐后续 content_block_delta 的 partial_json（事件里只有 index）
            key = str(item.get("index", block.get("id") or 0))
            slot = tool_calls.setdefault(key, {"id": block.get("id") or "", "name": "", "arguments": ""})
            slot["name"] = block.get("name") or slot["name"]
            slot["id"] = block.get("id") or slot["id"]
            if block.get("input"):
                slot["arguments"] = json.dumps(block["input"], ensure_ascii=False)
        elif block.get("type") == "text" and block.get("text"):
            out.append(block["text"])
        elif block.get("type") == "thinking" and block.get("thinking"):
            reason.append(block["thinking"])
    elif etype == "content_block_delta":
        delta = item.get("delta") or {}
        if isinstance(delta, dict):
            piece = delta.get("text")
            if isinstance(piece, str) and piece:
                out.append(piece)
            think = delta.get("thinking")
            if isinstance(think, str) and think:
                reason.append(think)
            partial = delta.get("partial_json")
            if isinstance(partial, str) and partial:
                key = str(item.get("index", 0))
                slot = tool_calls.setdefault(key, {"id": "", "name": "", "arguments": ""})
                slot["arguments"] += partial
    elif etype is None:
        # 非流式 Messages 响应
        for block in item.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text"):
                out.append(block["text"])
            elif block.get("type") == "thinking" and block.get("thinking"):
                reason.append(block["thinking"])
            elif block.get("type") == "tool_use":
                key = str(block.get("id") or block.get("name") or len(tool_calls))
                tool_calls.setdefault(key, {
                    "id": block.get("id") or "",
                    "name": block.get("name") or "",
                    "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                })


# --------------------------------------------------------------------------- 工具
def _base_time(con: sqlite3.Connection) -> float:
    try:
        row = con.execute("SELECT MIN(start_time) FROM result").fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0
    except (sqlite3.Error, TypeError, ValueError):
        return 0.0


def _rel_time(ts: Any, base: float) -> str:
    """evalscope 存的是 perf_counter 秒数，展示为相对本次运行起点的偏移。"""
    try:
        delta = float(ts) - float(base)
    except (TypeError, ValueError):
        return ""
    return f"+{delta:.3f}s"


def _round(v: Any, nd: int = 4) -> Optional[float]:
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return None


def _truncate_text(text: str) -> str:
    if len(text) <= MAX_DETAIL_CHARS:
        return text
    return text[:MAX_DETAIL_CHARS] + f"\n...（已截断，原始长度 {len(text)} 字符）"


def _truncate_obj(obj: Any) -> Any:
    try:
        dumped = json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(obj)[:MAX_DETAIL_CHARS]
    if len(dumped) <= MAX_DETAIL_CHARS:
        return obj
    return dumped[:MAX_DETAIL_CHARS] + f"\n...（已截断，原始长度 {len(dumped)} 字符）"


def _headers_obj(raw: Any) -> Dict[str, str]:
    """解析 ``response_headers`` 列。

    老库没有该列（补列前产生的记录）或非 JSON 时返回空 dict，前端据此显示
    「该记录没有响应头」，不会报错。
    """
    if not raw:
        return {}
    if isinstance(raw, dict):
        data = raw
    else:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}
