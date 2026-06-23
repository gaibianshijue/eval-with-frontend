"""Flask 路由层 —— 全部业务下沉到 runner.BenchmarkRunner。

新架构特点（与旧版差异）：
- 不再渲染 / 落盘 / 调用 bash 脚本
- 不再依赖 jq / bc / Git Bash / WSL
- evalscope 直接 in-process 调用，日志通过 logging.Handler 桥接到 SSE
- api_key 仅经 Arguments 传给 evalscope HTTP 客户端，不写任何文件
"""

from __future__ import annotations

import queue
import threading
from collections import deque
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, send_file

from runner import BenchmarkRunner

# ---------- 路径 ----------
WEB_DIR = Path(__file__).resolve().parent
PROJECT_DIR = WEB_DIR.parent
HISTORY_DIR = WEB_DIR / "history"
HISTORY_DIR.mkdir(exist_ok=True)

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
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
