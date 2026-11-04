"""Flask 路由层 —— 全部业务下沉到 runner.BenchmarkRunner。

新架构特点（与旧版差异）：
- 不再渲染 / 落盘 / 调用 bash 脚本
- 不再依赖 jq / bc / Git Bash / WSL
- evalscope 直接 in-process 调用，日志通过 logging.Handler 桥接到 SSE
- api_key 仅经 Arguments 传给 evalscope HTTP 客户端，不写任何文件
"""

from __future__ import annotations

import json
import queue
import threading
from collections import deque
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, send_file

import request_store
from evalscope_schema import describe_arguments, parse_perf_command
from runner import BenchmarkRunner

# ---------- 路径 ----------
WEB_DIR = Path(__file__).resolve().parent
PROJECT_DIR = WEB_DIR.parent
HISTORY_DIR = WEB_DIR / "history"
HISTORY_DIR.mkdir(exist_ok=True)
DATASETS_DIR = WEB_DIR / "datasets"

# ---------- Flask ----------
app = Flask(__name__, template_folder=str(WEB_DIR / "templates"))

# ---------- SSE broadcast ----------
_log_buffer: deque[str] = deque(maxlen=600)
_log_lock = threading.Lock()
_subscribers: list[queue.Queue] = []
_sub_lock = threading.Lock()


def _broadcast(line: str) -> None:
    """供 runner 调用：写入环形缓冲 + 推给所有 SSE 订阅者。"""
    with _log_lock:
        _log_buffer.append(line)
    with _sub_lock:
        dead = []
        for q in _subscribers:
            try:
                q.put_nowait(line)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _subscribers.remove(q)


def _sse(line: str) -> str:
    safe = line.replace("\r\n", "\n").replace("\r", "\n")
    parts = [f"data: {p}\n" for p in safe.split("\n")]
    parts.append("\n")
    return "".join(parts)


# ---------- 单例 runner ----------
_runner = BenchmarkRunner(history_root=HISTORY_DIR, broadcast=_broadcast)


# ---------- routes ----------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/state", methods=["GET"])
def state():
    s = _runner.status()
    return jsonify({
        "running": s["running"],
        "state": s["state"],
        "started_at": s["started_at"],
        "run_dir": s["run_dir"],
        "csv_path": s["csv_path"],
    })


@app.route("/start", methods=["POST"])
def start():
    cfg = request.get_json(force=True, silent=True) or {}
    # 清空旧日志缓冲（让前端看到清晰的新一轮起点）
    with _log_lock:
        _log_buffer.clear()
    ok, msg = _runner.start(cfg)
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400
    return jsonify({"ok": True, "run_id": msg})


@app.route("/stop", methods=["POST"])
def stop():
    _runner.stop()
    return jsonify({"ok": True})


@app.route("/status", methods=["GET"])
def status():
    return jsonify(_runner.status())


@app.route("/results", methods=["GET"])
def results():
    """返回当前运行累积的所有结果行（供前端实时展示）。"""
    return jsonify(_runner.results())


@app.route("/evalscope/schema", methods=["GET"])
def evalscope_schema():
    """evalscope perf 全量参数元信息（字段 / 类型 / 默认值 / CLI flag / 帮助 / 分组）。"""
    try:
        return jsonify({"ok": True, **describe_arguments()})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/evalscope/parse-command", methods=["POST"])
def evalscope_parse_command():
    """解析一条 `evalscope perf ...` 命令，回填前端表单。"""
    body = request.get_json(force=True, silent=True) or {}
    command = body.get("command", "")
    try:
        return jsonify({"ok": True, **parse_perf_command(command)})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"解析异常: {e}"}), 500


@app.route("/datasets", methods=["GET"])
def datasets():
    """列出 datasets/manifest.json 里登记的本地数据集（供前端一键选择）。"""
    manifest = DATASETS_DIR / "manifest.json"
    if not manifest.exists():
        return jsonify({"ok": True, "datasets": [],
                        "hint": "尚未生成，执行 python mock_dataset.py 后刷新"})
    try:
        meta = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return jsonify({"ok": False, "error": f"manifest 读取失败: {e}"}), 500
    items = []
    for entry in meta.get("datasets") or []:
        path = DATASETS_DIR / str(entry.get("file", ""))
        if not path.exists():
            continue
        items.append({**entry, "path": str(path), "bytes": path.stat().st_size})
    return jsonify({
        "ok": True,
        "source": meta.get("source"),
        "source_file": meta.get("source_file"),
        "source_messages": meta.get("source_messages"),
        "generated_at": meta.get("generated_at"),
        "datasets": items,
    })


@app.route("/db/runs", methods=["GET"])
def db_runs():
    """列出 history 下所有 sub-test 的 benchmark_data.db。"""
    try:
        return jsonify({"ok": True, "runs": request_store.list_databases(HISTORY_DIR)})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/db/requests", methods=["GET"])
def db_requests():
    """分页查询单次请求记录，支持响应码 / 关键字筛选。"""
    db = request.args.get("db", "")
    path = request_store.resolve_db(HISTORY_DIR, db)
    if not path:
        return jsonify({"ok": False, "error": "db 文件不存在或不在 history 目录内"}), 404
    try:
        data = request_store.query(
            path,
            status=request.args.get("status", ""),
            keyword=request.args.get("q", ""),
            page=request.args.get("page", 1),
            size=request.args.get("size", 20),
        )
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"读取 db 失败: {e}"}), 500
    return jsonify({"ok": True, "db": db, **data})


@app.route("/db/request", methods=["GET"])
def db_request():
    """单条请求详情：请求体 + 解码后的响应内容（含 tool_calls）。"""
    db = request.args.get("db", "")
    path = request_store.resolve_db(HISTORY_DIR, db)
    if not path:
        return jsonify({"ok": False, "error": "db 文件不存在或不在 history 目录内"}), 404
    try:
        rowid = int(request.args.get("id", ""))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "id 必须是整数"}), 400
    try:
        data = request_store.detail(path, rowid)
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"读取记录失败: {e}"}), 500
    if data is None:
        return jsonify({"ok": False, "error": f"记录 {rowid} 不存在"}), 404
    return jsonify({"ok": True, "db": db, **data})


@app.route("/download/csv", methods=["GET"])
def download_csv():
    """下载当前运行的 CSV 结果文件。"""
    csv_path = _runner.results().get("csv_path")
    if not csv_path:
        return jsonify({"ok": False, "error": "暂无结果文件"}), 404
    p = Path(csv_path)
    if not p.exists():
        return jsonify({"ok": False, "error": f"文件不存在: {csv_path}"}), 404
    return send_file(p, as_attachment=True, download_name=p.name)


@app.route("/stream")
def stream():
    """SSE：先回放近期缓冲，然后持续推送。"""
    def gen():
        q: queue.Queue = queue.Queue(maxsize=4000)
        with _sub_lock:
            _subscribers.append(q)
        with _log_lock:
            backlog = list(_log_buffer)
        for ln in backlog:
            yield _sse(ln)
        try:
            while True:
                try:
                    line = q.get(timeout=15)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                yield _sse(line)
        except GeneratorExit:
            pass
        finally:
            with _sub_lock:
                if q in _subscribers:
                    _subscribers.remove(q)

    return Response(gen(), headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })


if __name__ == "__main__":
    print(f"[web_demo] project dir: {PROJECT_DIR}")
    print(f"[web_demo] history dir: {HISTORY_DIR}")
    print(f"[web_demo] backend:     evalscope Python SDK (in-process)")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
