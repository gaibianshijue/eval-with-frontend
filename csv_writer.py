"""CSV 输出 —— 字段格式与 eval_bench0522.sh 对齐（新增 api_key_index 列）。

表头 22 列（按顺序）：
test_timestamp, model_name, api_key_index, task_type, repeat_num, Prompt_length, Max_tokens,
Number_of_concurrency, Total_requests, Succeed_requests, Avg_Latency(s),
Latency_P90(s), TTFT_Avg(s), TTFT_P90(s), TPOT_Avg(s), Avg_Inter_Token_Latency(s),
Avg_Input_Tokens, Avg_Output_Tokens, Output_through(tok/s), RPS(req/s),
Total_token_through(tok/s), output_dir
"""

from __future__ import annotations

import csv
import statistics
import threading
from datetime import datetime
from pathlib import Path
from typing import Iterable

HEADER = [
    "test_timestamp", "model_name", "api_key_index", "task_type", "repeat_num",
    "Prompt_length", "Max_tokens", "Number_of_concurrency",
    "Total_requests", "Succeed_requests",
    "Avg_Latency(s)", "Latency_P90(s)",
    "TTFT_Avg(s)", "TTFT_P90(s)",
    "TPOT_Avg(s)", "Avg_Inter_Token_Latency(s)",
    "Avg_Input_Tokens", "Avg_Output_Tokens",
    "Output_through(tok/s)", "RPS(req/s)", "Total_token_through(tok/s)",
    "output_dir",
]


def _fmt(v) -> str:
    """与 jq -r 的字符串化保持一致：None/缺失输出空串；浮点全精度。"""
    if v is None:
        return ""
    if isinstance(v, float):
        # 6 位小数足够覆盖原脚本 scale=6 的平均值
        return f"{v:.6f}".rstrip("0").rstrip(".") or "0"
    return str(v)


def _p90_seconds(percentiles, metric_field: str, *, is_ms_field: bool = False) -> float:
    """从 PercentileResult 取 P90，并按需把毫秒换算为秒。

    PercentileRow 里 ttft/tpot/itl 是毫秒（alias 后缀 (ms)），latency 是秒（(s)）。
    CSV 表头统一用秒，所以毫秒字段要 / 1000。
    """
    try:
        val = percentiles.get_p("90%", metric_field)
    except Exception:
        return 0.0
    if val is None:
        return 0.0
    return float(val) / 1000.0 if is_ms_field else float(val)


def _to_row(
    task_type: str,
    repeat_label: str,
    prompt_length: int,
    max_tokens: int,
    metrics,                       # BenchmarkSummary
    percentiles,                   # PercentileResult
    model_name: str,
    output_dir: str,
    api_key_index: int = 1,       # API Key 序号（从 1 开始），多用户标识
) -> list[str]:
    return [
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        model_name,
        str(api_key_index),
        task_type,
        repeat_label,
        _fmt(prompt_length),
        _fmt(max_tokens),
        _fmt(getattr(metrics, "concurrency", 0)),
        _fmt(getattr(metrics, "total_requests", 0)),
        _fmt(getattr(metrics, "succeed_requests", 0)),
        _fmt(getattr(metrics, "avg_latency", 0.0)),
        _fmt(_p90_seconds(percentiles, "latency", is_ms_field=False)),
        _fmt(getattr(metrics, "avg_ttft", 0.0)),
        _fmt(_p90_seconds(percentiles, "ttft", is_ms_field=True)),
        _fmt(getattr(metrics, "avg_tpot", 0.0)),
        _fmt(getattr(metrics, "avg_itl", 0.0)),
        _fmt(getattr(metrics, "avg_input_tokens", 0.0)),
        _fmt(getattr(metrics, "avg_output_tokens", 0.0)),
        _fmt(getattr(metrics, "output_token_throughput", 0.0)),
        _fmt(getattr(metrics, "request_throughput", 0.0)),
        _fmt(getattr(metrics, "total_token_throughput", 0.0)),
        output_dir,
    ]


class CsvWriter:
    """逐行写出 CSV；自带表头。同一文件多次 open 用 append 模式追加。线程安全。"""

    def __init__(self, path: Path, model_name: str):
        self.path = path
        self.model_name = model_name
        self._write_lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            with self.path.open("w", encoding="utf-8", newline="") as f:
                csv.writer(f).writerow(HEADER)

    def write_run(
        self, *,
        task_type: str,
        repeat_num: int,
        prompt_length: int,
        max_tokens: int,
        metrics,
        percentiles,
        output_dir: str,
        api_key_index: int = 1,
    ) -> dict:
        row = _to_row(
            task_type, str(repeat_num), prompt_length, max_tokens,
            metrics, percentiles, self.model_name, output_dir,
            api_key_index=api_key_index,
        )
        self._append(row)
        return dict(zip(HEADER, row))

    def write_avg(
        self, *,
        task_type: str,
        prompt_length: int,
        max_tokens: int,
        runs: Iterable[dict],     # list of {"metrics":..., "percentiles":...}
        api_key_index: int = 1,
    ) -> dict:
        """对一组 repeat 结果取均值。模仿原脚本只输出数值列，分类列复用第一条。"""
        runs = list(runs)
        if not runs:
            return {}

        # 收集每个数值字段的 values，none 跳过
        def _avg(getter):
            vals = []
            for r in runs:
                try:
                    v = getter(r)
                    if v is not None:
                        vals.append(float(v))
                except Exception:
                    continue
            return statistics.fmean(vals) if vals else 0.0

        first = runs[0]
        m0 = first["metrics"]

        row = [
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            self.model_name,
            str(api_key_index),
            task_type,
            "AVG",
            _fmt(prompt_length),
            _fmt(max_tokens),
            _fmt(getattr(m0, "concurrency", 0)),
            _fmt(getattr(m0, "total_requests", 0)),
            _fmt(getattr(m0, "succeed_requests", 0)),
            _fmt(_avg(lambda r: r["metrics"].avg_latency)),
            _fmt(_avg(lambda r: _p90_seconds(r["percentiles"], "latency"))),
            _fmt(_avg(lambda r: r["metrics"].avg_ttft)),
            _fmt(_avg(lambda r: _p90_seconds(r["percentiles"], "ttft", is_ms_field=True))),
            _fmt(_avg(lambda r: r["metrics"].avg_tpot)),
            _fmt(_avg(lambda r: r["metrics"].avg_itl)),
            _fmt(_avg(lambda r: r["metrics"].avg_input_tokens)),
            _fmt(_avg(lambda r: r["metrics"].avg_output_tokens)),
            _fmt(_avg(lambda r: r["metrics"].output_token_throughput)),
            _fmt(_avg(lambda r: r["metrics"].request_throughput)),
            _fmt(_avg(lambda r: r["metrics"].total_token_throughput)),
            "AVERAGE",
        ]
        self._append(row)
        return dict(zip(HEADER, row))

    def _append(self, row: list[str]) -> None:
        with self._write_lock:
            with self.path.open("a", encoding="utf-8", newline="") as f:
                csv.writer(f).writerow(row)
