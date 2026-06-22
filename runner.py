"""BenchmarkRunner —— 用 evalscope Python SDK 串行跑完所有配置组合。

替代原 shell 脚本的 4 重循环 + jq 解析 + CSV 写入 + 进度/日志推送，全部 in-process。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from evalscope.perf.arguments import Arguments
from evalscope.perf.main import run_perf_benchmark
from evalscope.utils.logger import get_logger as get_evalscope_logger

from csv_writer import CsvWriter


# ---------- 配置 dataclass（轻量校验，区别于 Arguments 的完整字段）----------
@dataclass(frozen=True)
class IOGroup:
    input: int
    output: int


@dataclass(frozen=True)
class CRGroup:
    parallel: int
    number: int


@dataclass
class RunConfig:
    model_name: str
    api_url: str
    tokenizer_path: str
    openai_api_key: str
    openclaw_dataset_path: str
    openclaw_dataset_name: str
    test_repeats: int
    log_every_n_query: int
    prefix_length: int
    wait_between_tests: int
    task_types: list[str]
    input_output_groups: list[IOGroup]
    concurrency_request_groups: list[CRGroup]

    @classmethod
    def from_dict(cls, d: dict) -> "RunConfig":
        return cls(
            model_name=str(d.get("model_name", "")).strip(),
            api_url=str(d.get("api_url", "")).strip(),
            tokenizer_path=str(d.get("tokenizer_path", "")).strip(),
            openai_api_key=str(d.get("openai_api_key", "")),
            openclaw_dataset_path=str(d.get("openclaw_dataset_path", "")).strip(),
            openclaw_dataset_name=str(d.get("openclaw_dataset_name", "line_by_line")).strip() or "line_by_line",
            test_repeats=int(d.get("test_repeats", 1) or 1),
            log_every_n_query=int(d.get("log_every_n_query", 500) or 500),
            prefix_length=int(d.get("prefix_length", 0) or 0),
            wait_between_tests=int(d.get("wait_between_tests", 0) or 0),
            task_types=list(d.get("task_types") or []),
            input_output_groups=[IOGroup(int(g["input"]), int(g["output"]))
                                 for g in (d.get("input_output_groups") or [])],
            concurrency_request_groups=[CRGroup(int(g["parallel"]), int(g["number"]))
                                        for g in (d.get("concurrency_request_groups") or [])],
        )

    def to_safe_dict(self) -> dict:
        """落盘用，脱敏 api_key。"""
        d = asdict(self)
        if d.get("openai_api_key"):
            d["openai_api_key"] = "***"
        return d

    def validate(self) -> Optional[str]:
        if not self.model_name:
            return "model_name 不能为空"
        if not self.api_url:
            return "api_url 不能为空"
        if not self.task_types:
            return "task_types 至少选一项"
        for t in self.task_types:
            if t not in ("random", "openclaw"):
                return f"未知 task_type: {t}"
            if t == "openclaw" and not self.openclaw_dataset_path:
                return "选择了 openclaw 任务但未填写 openclaw_dataset_path"
        if not self.input_output_groups:
            return "input_output_groups 至少需要一行"
        if not self.concurrency_request_groups:
            return "concurrency_request_groups 至少需要一行"
        if self.test_repeats < 1:
            return "test_repeats 必须 >= 1"
        return None


# ---------- 日志桥：evalscope logger → SSE broadcast ----------
class SSELogHandler(logging.Handler):
    def __init__(self, broadcast: Callable[[str], None]):
        super().__init__()
        self.broadcast = broadcast
        self.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s | %(message)s", "%H:%M:%S"
        ))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.broadcast(self.format(record))
        except Exception:  # noqa: BLE001
            self.handleError(record)


# ---------- BenchmarkRunner ----------
class BenchmarkRunner:
    """单例：同一时刻只允许一个任务在跑。"""

    def __init__(self, history_root: Path, broadcast: Callable[[str], None]):
        self.history_root = history_root
        self.history_root.mkdir(parents=True, exist_ok=True)
        self._broadcast = broadcast

        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._state = "idle"           # idle / running / completed / stopped / error
        self._error: str = ""
        self._run_dir: Optional[Path] = None
        self._csv_path: Optional[Path] = None
        self._started_at: Optional[str] = None
        self._progress: dict = self._empty_progress()
        self._log_handler: Optional[SSELogHandler] = None

    # ---------- public ----------
    def start(self, config_dict: dict) -> tuple[bool, str]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False, "已有任务在运行，请先停止"

            try:
                cfg = RunConfig.from_dict(config_dict)
            except (KeyError, ValueError, TypeError) as e:
                return False, f"配置解析失败: {e}"

            err = cfg.validate()
            if err:
                return False, err

            # 初始化 run 目录
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            self._run_dir = self.history_root / ts
            self._run_dir.mkdir(parents=True, exist_ok=True)
            (self._run_dir / "config.json").write_text(
                json.dumps(cfg.to_safe_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._csv_path = self._run_dir / f"results_{cfg.model_name}_{ts}.csv"

            # 接入 evalscope logger
            self._attach_logger()

            self._stop_event.clear()
            self._state = "running"
            self._error = ""
            self._started_at = datetime.now().isoformat(timespec="seconds")
            total = (len(cfg.task_types)
                     * len(cfg.input_output_groups)
                     * len(cfg.concurrency_request_groups)
                     * cfg.test_repeats)
            self._progress = self._empty_progress()
            self._progress["total"] = total
            self._progress["run_id"] = ts

            self._thread = threading.Thread(
                target=self._worker, args=(cfg,), name="BenchmarkRunner", daemon=True,
            )
            self._thread.start()
            return True, ts

    def stop(self) -> None:
        self._stop_event.set()
        self._broadcast("[user] 已请求停止 —— 当前测试会跑完，下一个测试前退出")

    def status(self) -> dict:
        prog = dict(self._progress)
        total = prog.get("total") or 0
        cur = prog.get("current") or 0
        prog["_current"] = cur
        prog["_total"] = total
        prog["_percent"] = int(cur * 100 / total) if total else 0
        # 兼容旧前端：中文字段
        prog["任务"] = prog.get("task_type", "")
        prog["IO配置"] = prog.get("io", "")
        prog["并发"] = prog.get("concurrency", "")
        prog["重复"] = prog.get("repeat", "")
        prog["预计剩余"] = self._eta()
        return {
            "state": self._state,
            "running": self._thread is not None and self._thread.is_alive(),
            "started_at": self._started_at,
            "error": self._error,
            "csv_path": str(self._csv_path) if self._csv_path else None,
            "run_dir": str(self._run_dir) if self._run_dir else None,
            "fields": prog,                # 前端读取的字段都在这里
        }

    # ---------- internal ----------
    def _empty_progress(self) -> dict:
        return {
            "current": 0, "total": 0,
            "task_type": "", "io": "",
            "concurrency": "", "repeat": "",
        }

    def _eta(self) -> str:
        if not self._started_at:
            return "--"
        try:
            start_ts = datetime.fromisoformat(self._started_at).timestamp()
        except Exception:  # noqa: BLE001
            return "--"
        cur = self._progress.get("current") or 0
        total = self._progress.get("total") or 0
        if cur < 1 or total < 1:
            return "计算中..."
        elapsed = max(time.time() - start_ts, 1.0)
        avg = elapsed / cur
        remaining = int(avg * max(total - cur, 0))
        h, rem = divmod(remaining, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    def _attach_logger(self) -> None:
        if self._log_handler:
            return
        handler = SSELogHandler(self._broadcast)
        handler.setLevel(logging.INFO)
        # evalscope 自家 logger
        get_evalscope_logger().addHandler(handler)
        # root logger（兜底其他子库的输出）
        logging.getLogger().addHandler(handler)
        self._log_handler = handler

    def _detach_logger(self) -> None:
        if not self._log_handler:
            return
        try:
            get_evalscope_logger().removeHandler(self._log_handler)
        except Exception:  # noqa: BLE001
            pass
        try:
            logging.getLogger().removeHandler(self._log_handler)
        except Exception:  # noqa: BLE001
            pass
        self._log_handler = None

    def _sleep_with_stop(self, seconds: int) -> None:
        end = time.time() + max(seconds, 0)
        while time.time() < end:
            if self._stop_event.is_set():
                return
            time.sleep(0.5)

    def _build_arguments(self, cfg: RunConfig, task_type: str, io: IOGroup, cr: CRGroup) -> Arguments:
        common = dict(
            model=cfg.model_name,
            url=cfg.api_url,
            api="openai",
            api_key=cfg.openai_api_key or None,
            parallel=cr.parallel,
            number=cr.number,
            max_tokens=io.output,
            log_every_n_query=cfg.log_every_n_query,
            prefix_length=cfg.prefix_length,
            tokenizer_path=cfg.tokenizer_path or None,
            read_timeout=600,
            connect_timeout=30,
            extra_args={"ignore_eos": True},
            enable_progress_tracker=True,
            outputs_dir=str(self._run_dir / "outputs"),
            debug=False,
        )
        if task_type == "random":
            return Arguments(
                **common,
                dataset="random",
                min_prompt_length=io.input,
                max_prompt_length=io.input,
            )
        # openclaw
        return Arguments(
            **common,
            dataset=cfg.openclaw_dataset_name,
            dataset_path=cfg.openclaw_dataset_path,
        )

    def _worker(self, cfg: RunConfig) -> None:
        writer = CsvWriter(self._csv_path, cfg.model_name)
        total = self._progress["total"]
        counter = 0
        self._broadcast(f"=== 开始批量测试 @ {self._started_at} 共 {total} 个测试 ===")
        self._broadcast(f"=== 结果文件: {self._csv_path} ===")

        try:
            for task_type in cfg.task_types:
                self._broadcast(f"--- 任务类型: {task_type} ---")
                for io in cfg.input_output_groups:
                    for cr in cfg.concurrency_request_groups:
                        run_results = []
                        for repeat in range(1, cfg.test_repeats + 1):
                            if self._stop_event.is_set():
                                self._broadcast("[user] 收到停止信号，退出")
                                self._state = "stopped"
                                return
                            counter += 1
                            self._progress.update({
                                "current": counter,
                                "task_type": task_type,
                                "io": f"输入{io.input}/输出{io.output}",
                                "concurrency": f"并发{cr.parallel}",
                                "repeat": f"{repeat}/{cfg.test_repeats}",
                            })
                            self._broadcast(
                                f"=== 测试 {counter}/{total}: "
                                f"task={task_type} repeat={repeat}/{cfg.test_repeats} "
                                f"input={io.input} output={io.output} "
                                f"parallel={cr.parallel} number={cr.number} ==="
                            )

                            try:
                                args = self._build_arguments(cfg, task_type, io, cr)
                                results = run_perf_benchmark(args)
                            except Exception as e:  # noqa: BLE001
                                self._broadcast(f"[error] evalscope 调用失败: {e}")
                                self._broadcast(traceback.format_exc())
                                continue

                            key = f"parallel_{cr.parallel}_number_{cr.number}"
                            run = results.get(key) or next(iter(results.values()), None)
                            if not run or "metrics" not in run:
                                self._broadcast(f"[warn] 未拿到 {key} 的结果，跳过 CSV")
                                continue

                            try:
                                row = writer.write_run(
                                    task_type=task_type,
                                    repeat_num=repeat,
                                    prompt_length=io.input,
                                    max_tokens=io.output,
                                    metrics=run["metrics"],
                                    percentiles=run["percentiles"],
                                    output_dir=str(self._run_dir / "outputs"),
                                )
                                run_results.append(run)
                                self._broadcast(
                                    f"[ok] {key}: TTFT={row['TTFT_Avg(s)']}s "
                                    f"TPOT={row['TPOT_Avg(s)']}s "
                                    f"OutThr={row['Output_through(tok/s)']}tok/s"
                                )
                            except Exception as e:  # noqa: BLE001
                                self._broadcast(f"[error] 写 CSV 失败: {e}")

                            if counter < total and cfg.wait_between_tests > 0:
                                self._broadcast(f"--- 等待 {cfg.wait_between_tests}s 进入下一测试 ---")
                                self._sleep_with_stop(cfg.wait_between_tests)

                        # 一组 cr 的所有 repeat 跑完，算 AVG
                        if cfg.test_repeats > 1 and run_results:
                            try:
                                writer.write_avg(
                                    task_type=task_type,
                                    prompt_length=io.input,
                                    max_tokens=io.output,
                                    runs=run_results,
                                )
                                self._broadcast(f"[avg] 写入 {len(run_results)} 次重复的平均值")
                            except Exception as e:  # noqa: BLE001
                                self._broadcast(f"[error] 写 AVG 失败: {e}")

            self._state = "completed"
            self._broadcast(f"=== 全部完成 @ {datetime.now().isoformat(timespec='seconds')} ===")
            self._broadcast(f"=== CSV: {self._csv_path} ===")
        except Exception as e:  # noqa: BLE001
            self._state = "error"
            self._error = str(e)
            self._broadcast(f"[fatal] worker 异常: {e}")
            self._broadcast(traceback.format_exc())
        finally:
            self._detach_logger()
