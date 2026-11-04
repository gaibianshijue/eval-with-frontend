"""BenchmarkRunner —— 用 evalscope Python SDK 串行跑完所有配置组合。

替代原 shell 脚本的 4 重循环 + jq 解析 + CSV 写入 + 进度/日志推送，全部 in-process。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from evalscope.perf.arguments import Arguments
from evalscope.perf.main import run_perf_benchmark
from evalscope.perf.multi_turn_args import MultiTurnArgs
from evalscope.perf.plugin.registry import ApiRegistry
from evalscope.utils.logger import get_logger as get_evalscope_logger

from csv_writer import CsvWriter
from evalscope_schema import MULTI_TURN_DATASETS, MULTI_TURN_PATH_REQUIRED

# 注册 anthropic 压测插件（import 副作用），缺失时只影响 anthropic 协议
try:  # pragma: no cover
    import perf_anthropic  # noqa: F401
except Exception as _e:  # noqa: BLE001
    get_evalscope_logger().warning(f"anthropic 插件加载失败: {_e}")

# 注册 perf 数据集插件（import 副作用）：
#   * mock_messages：重放真实 agent 轨迹
#   * random_multi_turn：清洗随机 token 解码出的控制/零宽字符（同名覆盖官方插件）
try:  # pragma: no cover
    import perf_datasets  # noqa: F401
except Exception as _e:  # noqa: BLE001
    get_evalscope_logger().warning(f"perf 数据集插件加载失败: {_e}")


#: 主流协议（其余需在 ApiRegistry 中已注册）
KNOWN_APIS = {"openai", "openai_responses", "anthropic", "dashscope", "local", "local_vllm"}


# ---------- 自定义异常 ----------
class StopRequested(Exception):
    """用户请求停止时抛出，用于从阻塞的 evalscope 调用中跳出。"""
    pass


def _clean_evalscope_args(raw) -> dict:
    """清洗高级透传参数：仅保留 Arguments 已知字段，去掉批量表单接管的字段。"""
    from evalscope_schema import MANAGED_FIELDS

    if isinstance(raw, str):
        raw = json.loads(raw) if raw.strip() else {}
    if not isinstance(raw, dict):
        return {}
    valid = set(Arguments.model_fields.keys())
    out: dict = {}
    for k, v in raw.items():
        if k not in valid or k in MANAGED_FIELDS:
            continue
        if v is None or v == "" or v == [] or v == {}:
            continue
        out[str(k)] = v
    return out


def _coerce_tools(raw) -> list[dict]:
    """把前端传来的 tools（JSON 字符串 / dict / list）归一化为 list[dict]。"""
    if not raw:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"tools 不是合法 JSON（{e.msg}，位置 {e.pos}）") from e
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        raise ValueError("tools 必须是 JSON 数组")
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict) or not item:
            raise ValueError("tools 数组的每一项必须是对象")
        fn = item.get("function") if isinstance(item.get("function"), dict) else item
        if not fn.get("name"):
            raise ValueError("tools 每一项都需要 function.name")
        if "input_schema" not in item and not isinstance(fn.get("parameters", {}), dict):
            raise ValueError(f"tools[{fn.get('name')}].function.parameters 必须是 JSON 对象")
        out.append(item)
    return out


#: 给 evalscope 的 result 表补的列（原表没有 status_code / error，前端无法按响应码筛选）
DB_EXTRA_COLUMNS = (
    ("status_code", "INTEGER"),
    ("error", "TEXT"),
    ("trace_id", "TEXT"),
    ("turns", "INTEGER"),
    ("is_first_turn", "INTEGER"),
    ("generated_text", "TEXT"),
    ("response_headers", "TEXT"),
)

_DB_PATCHED = False


# ---------- 子任务（单次 run_perf_benchmark）的请求级进度 ----------
# 每个 sub-test 在自己的线程里跑 asyncio loop，metrics_consumer 的
# insert_benchmark_data 也在同一线程执行，因此用线程 id 就能把「完成一个请求」
# 精确归属到对应的子任务；多 key 并发时各线程独立登记，前端读到的是汇总值。
_SUB_TRACK: dict[int, dict] = {}
_SUB_TRACK_LOCK = threading.Lock()


def _sub_register(total: int) -> None:
    """进入一个子测试：登记本轮要发多少个请求。"""
    tid = threading.get_ident()
    with _SUB_TRACK_LOCK:
        _SUB_TRACK[tid] = {"total": max(int(total or 0), 0), "done": 0}


def _sub_unregister() -> None:
    tid = threading.get_ident()
    with _SUB_TRACK_LOCK:
        _SUB_TRACK.pop(tid, None)


def _sub_progress() -> tuple[int, int]:
    """汇总所有活跃子任务的 (已完成请求数, 总请求数)。"""
    with _SUB_TRACK_LOCK:
        done = sum(v["done"] for v in _SUB_TRACK.values())
        total = sum(v["total"] for v in _SUB_TRACK.values())
    return done, total


def _sub_tick(data) -> None:
    """一个请求落库后调用一次，口径与 evalscope 的进度条一致。

    - warmup 请求不计入（metrics_consumer 会 ``continue``，不会写库）；
    - 多轮模式下同一次对话有多个 turn，只在 ``is_last_turn`` 时算一次，
      否则 ``done`` 会是 ``number`` 的数倍。
    """
    if getattr(data, "is_warmup", False):
        return
    if not getattr(data, "is_last_turn", False) and getattr(data, "input_num_turns", 0):
        return
    tid = threading.get_ident()
    with _SUB_TRACK_LOCK:
        rec = _SUB_TRACK.get(tid)
        if rec is not None:
            rec["done"] += 1


def _patch_evalscope_db() -> None:
    """扩展 evalscope 的 benchmark_data.db：补 status_code / error 等列。

    ``statistic_benchmark_metric`` 内部写库走的是 metrics_consumer 模块级名字，
    所以直接替换该模块的属性即可生效（必须在 run_perf_benchmark 之前调用一次）。
    """
    global _DB_PATCHED
    if _DB_PATCHED:
        return
    from evalscope.perf.core import metrics_consumer as mc

    orig_create = mc.create_result_table
    orig_insert = mc.insert_benchmark_data

    def create_result_table(cursor):
        orig_create(cursor)
        existing = {row[1] for row in cursor.execute("PRAGMA table_info(result)")}
        for name, typ in DB_EXTRA_COLUMNS:
            if name not in existing:
                cursor.execute(f"ALTER TABLE result ADD COLUMN {name} {typ}")

    def insert_benchmark_data(cursor, data):
        orig_insert(cursor, data)
        _sub_tick(data)
        status = getattr(data, "status_code", None)
        if status is None and getattr(data, "success", False):
            status = 200
        text = getattr(data, "generated_text", "") or ""
        # 响应头由插件挂在 BenchmarkData 的动态属性上（当前仅 anthropic 插件设置），
        # 其余协议的记录该列为 NULL，前端会提示「该协议未记录」。
        resp_headers = getattr(data, "response_headers", None)
        try:
            headers_json = json.dumps(resp_headers, ensure_ascii=False) if resp_headers else None
        except (TypeError, ValueError):
            headers_json = None
        cursor.execute(
            "UPDATE result SET status_code=?, error=?, trace_id=?, turns=?, is_first_turn=?, "
            "generated_text=?, response_headers=? WHERE rowid=last_insert_rowid()",
            (
                int(status) if isinstance(status, (int, float)) else None,
                (getattr(data, "error", "") or "")[:4000],
                getattr(data, "trace_id", "") or "",
                int(getattr(data, "input_num_turns", 0) or 0),
                int(bool(getattr(data, "is_first_turn", False))),
                str(text)[:200000],
                headers_json,
            ),
        )

    mc.create_result_table = create_result_table
    mc.insert_benchmark_data = insert_benchmark_data
    _DB_PATCHED = True


def _sub_key(task_type: str, io: IOGroup, cr: CRGroup, repeat: int, key_idx: int) -> str:
    """每个 sub-test 的输出子目录名（避免共用 benchmark_data.db 冲突）。"""
    raw = f"{task_type}_in{io.input}_out{io.output}_p{cr.parallel}_n{cr.number}_r{repeat}_k{key_idx + 1}"
    return re.sub(r"[^0-9A-Za-z._-]+", "-", raw)[:120]


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
    openai_api_keys: list[str]       # 支持多 API Key，模拟多用户
    openclaw_dataset_path: str
    openclaw_dataset_name: str
    test_repeats: int
    log_every_n_query: int
    prefix_length: int
    wait_between_tests: int
    task_types: list[str]
    input_output_groups: list[IOGroup]
    concurrency_request_groups: list[CRGroup]
    stream: bool = True
    manual_dataset: list[dict] = field(default_factory=list)  # [{question, answer}]，answer 可空
    # 协议：openai / openai_responses / anthropic / dashscope ...
    api: str = "openai"
    # 多轮对话压测
    multi_turn: bool = False
    min_turns: int = 1
    max_turns: int = 2
    first_turn_length: int = 0        # 0 = 用 evalscope 默认（65000）
    subsequent_turn_length: int = 0   # 0 = 用 evalscope 默认（500）
    # 工具调用（function calling）：OpenAI 形状，anthropic 协议由插件自动转换
    tools: list[dict] = field(default_factory=list)
    tool_choice: str = ""             # "" / auto / none / required / 具体函数名
    # evalscope.perf.Arguments 的高级透传参数（键名即 Arguments 字段名），
    # 由前端「高级参数」面板或 `evalscope perf` 命令解析得到。
    evalscope_args: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "RunConfig":
        # 兼容旧格式 openai_api_key（单字符串）→ 自动转为列表
        api_keys = d.get("openai_api_keys")
        if not api_keys:
            single = d.get("openai_api_key", "")
            api_keys = [single] if single else []
        else:
            api_keys = [k for k in api_keys if k]  # 过滤空字符串
        return cls(
            model_name=str(d.get("model_name", "")).strip(),
            api_url=str(d.get("api_url", "")).strip(),
            tokenizer_path=str(d.get("tokenizer_path", "")).strip(),
            openai_api_keys=api_keys,
            openclaw_dataset_path=str(d.get("openclaw_dataset_path", "")).strip(),
            openclaw_dataset_name=str(d.get("openclaw_dataset_name", "line_by_line")).strip() or "line_by_line",
            test_repeats=int(d.get("test_repeats", 1) or 1),
            log_every_n_query=int(d.get("log_every_n_query", 500) or 500),
            prefix_length=int(d.get("prefix_length", 0) or 0),
            wait_between_tests=int(d.get("wait_between_tests", 0) or 0),
            task_types=list(d.get("task_types") or []),
            # input/output 为 0 或缺失都表示「不限制」，不传对应的 evalscope 参数
            input_output_groups=[IOGroup(int(g.get("input") or 0), int(g.get("output") or 0))
                                 for g in (d.get("input_output_groups") or [])]
            or [IOGroup(0, 0)],
            concurrency_request_groups=[CRGroup(int(g["parallel"]), int(g["number"]))
                                        for g in (d.get("concurrency_request_groups") or [])],
            stream=bool(d.get("stream", True)),
            manual_dataset=list(d.get("manual_dataset") or []),
            api=str(d.get("api") or "openai").strip() or "openai",
            multi_turn=bool(d.get("multi_turn", False)),
            min_turns=int(d.get("min_turns", 1) or 1),
            max_turns=int(d.get("max_turns", 2) or 2),
            first_turn_length=int(d.get("first_turn_length", 0) or 0),
            subsequent_turn_length=int(d.get("subsequent_turn_length", 0) or 0),
            tools=_coerce_tools(d.get("tools")),
            tool_choice=str(d.get("tool_choice") or "").strip(),
            evalscope_args=_clean_evalscope_args(d.get("evalscope_args")),
        )

    def to_safe_dict(self) -> dict:
        """落盘用，脱敏 api_key。"""
        d = asdict(self)
        d["openai_api_keys"] = [
            (f"sk-***{k[-4:]}" if len(k) > 4 else "***") for k in d.get("openai_api_keys", [])
        ]
        # 高级参数里的第三方 key 同样脱敏
        adv = d.get("evalscope_args")
        if isinstance(adv, dict):
            for k in ("wandb_api_key", "swanlab_api_key"):
                if adv.get(k):
                    adv[k] = "***"
        # 移除旧字段（如果 asdict 包含了的话）
        d.pop("openai_api_key", None)
        return d

    def validate(self) -> Optional[str]:
        if not self.model_name:
            return "model_name 不能为空"
        if not self.api_url:
            return "api_url 不能为空"
        if not self.openai_api_keys:
            return "至少需要填写一个 API Key"
        for t in self.task_types:
            if t not in ("random", "openclaw", "manual"):
                return f"未知 task_type: {t}"
            if t == "random" and not self.tokenizer_path:
                return "选择了 random 任务但未填写 tokenizer_path（evalscope random dataset 需要 --tokenizer-path）"
            if t == "openclaw" and not self.openclaw_dataset_name:
                return "选择了 openclaw 任务但未填写 dataset 名称"
            if t == "openclaw" and self.openclaw_dataset_name in (
                "line_by_line", "custom", "custom_multi_turn", "mock_messages"
            ) and not self.openclaw_dataset_path:
                return f"dataset={self.openclaw_dataset_name} 需要填写数据集路径"
            if t == "manual":
                qs = [str(m.get("question", "")).strip()
                      for m in self.manual_dataset if str(m.get("question", "")).strip()]
                if not qs:
                    return "选择了 manual 任务但未填写任何问题（question 必填）"
        if self.api not in KNOWN_APIS and self.api not in ApiRegistry.all_classes():
            available = sorted(set(KNOWN_APIS) | set(ApiRegistry.all_classes()))
            return f"未知 api 协议: {self.api}（可用: {', '.join(available)}）"
        if self.tool_choice and not self.tools:
            return "填写了 tool_choice 但未配置 tools"
        if (not self.multi_turn and "openclaw" in self.task_types
                and self.openclaw_dataset_name in MULTI_TURN_DATASETS):
            return (f"dataset={self.openclaw_dataset_name} 是多轮数据集，"
                    "必须勾选多轮对话压测（multi_turn / --multi-turn）")
        if self.multi_turn:
            if self.api not in ApiRegistry.all_classes():
                return f"api={self.api} 未注册压测插件，无法使用多轮模式"
            if self.min_turns < 1 or self.max_turns < self.min_turns:
                return "多轮参数不合法：需满足 1 <= min_turns <= max_turns"
            if "manual" in self.task_types:
                return "多轮模式不支持 manual 任务，请改用 custom_multi_turn 数据集"
            if "random" in self.task_types and not self.tokenizer_path:
                return "多轮 + random 任务需要 tokenizer_path（random_multi_turn 依赖 tokenizer 生成上下文）"
            if "openclaw" in self.task_types:
                name = self.openclaw_dataset_name
                if name not in MULTI_TURN_DATASETS:
                    return (f"多轮模式下 dataset={name} 不是多轮数据集，可用: "
                            + ", ".join(sorted(MULTI_TURN_DATASETS)))
                if name in MULTI_TURN_PATH_REQUIRED and not self.openclaw_dataset_path:
                    return f"dataset={name} 需要填写数据集路径"
        if not self.task_types:
            return "task_types 至少选一项"
        if "random" in self.task_types and (not self.input_output_groups
                                            or self.input_output_groups[0].input <= 0):
            return "random 任务需要填写输入长度（0 或留空表示不限制，仅 dataset 任务可用）"
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
        self._manual_dataset_path: Optional[Path] = None
        self._started_at: Optional[str] = None
        self._progress: dict = self._empty_progress()
        self._log_handler: Optional[SSELogHandler] = None
        # 多 Key 并发的线程安全进度追踪
        self._progress_lock = threading.Lock()
        self._completed_count: int = 0
        # evalscope 内部使用全局 asyncio.Event，多线程并发调用会冲突，需串行化
        self._evalscope_lock = threading.Lock()
        # 结构化结果存储（供前端实时展示和下载）
        self._results: list[dict] = []
        self._results_lock = threading.Lock()

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

            # manual 任务：把问题落盘成 line_by_line 兼容的 jsonl（每行一个 JSON 字符串）
            if "manual" in cfg.task_types:
                self._manual_dataset_path = self._run_dir / "manual_dataset.jsonl"
                qs = [str(m.get("question", "")).strip()
                      for m in cfg.manual_dataset if str(m.get("question", "")).strip()]
                self._manual_dataset_path.write_text(
                    "\n".join(json.dumps(q, ensure_ascii=False) for q in qs) + ("\n" if qs else ""),
                    encoding="utf-8",
                )
            else:
                self._manual_dataset_path = None

            # 接入 evalscope logger
            self._attach_logger()

            self._stop_event.clear()
            self._state = "running"
            self._error = ""
            self._started_at = datetime.now().isoformat(timespec="seconds")
            num_keys = len(cfg.openai_api_keys) or 1
            per_key_total = (len(cfg.task_types)
                             * len(cfg.input_output_groups)
                             * len(cfg.concurrency_request_groups)
                             * cfg.test_repeats)
            total = per_key_total * num_keys
            self._progress = self._empty_progress()
            self._progress["total"] = total
            self._progress["api_key_count"] = num_keys
            self._progress["run_id"] = ts
            self._completed_count = 0
            with _SUB_TRACK_LOCK:
                _SUB_TRACK.clear()
            with self._results_lock:
                self._results = []

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
        # 请求级子进度：当前正在跑的子测试已完成/应发请求数（多 key 并发时为汇总值）
        sub_done, sub_total = _sub_progress()
        prog["_sub_current"] = sub_done
        prog["_sub_total"] = sub_total
        prog["_sub_percent"] = min(int(sub_done * 100 / sub_total), 100) if sub_total else 0
        # 兼容旧前端：中文字段
        prog["任务"] = prog.get("task_type", "")
        prog["IO配置"] = prog.get("io", "")
        prog["并发"] = prog.get("concurrency", "")
        prog["重复"] = prog.get("repeat", "")
        prog["预计剩余"] = self._eta()
        prog["用户数"] = prog.get("api_key_count", 1)
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
    def results(self) -> dict:
        """返回累积的结果行列表和 CSV 路径（供前端 /results 和 /download/csv 使用）。"""
        with self._results_lock:
            return {
                "results": list(self._results),
                "csv_path": str(self._csv_path) if self._csv_path else None,
            }

    def _broadcast_result(self, row_dict: dict) -> None:
        """通过 SSE 广播结构化结果，同时追加到 _results 列表。"""
        with self._results_lock:
            self._results.append(row_dict)
        # 用特殊前缀与普通日志区分，前端 JS 检测此前缀解析 JSON
        self._broadcast(f"__RESULT__:{json.dumps(row_dict, ensure_ascii=False)}")

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

    def _run_perf_benchmark_safe(self, args: Arguments, sub_total: int = 0) -> dict:
        """线程安全的 run_perf_benchmark 封装，支持通过 _stop_event 中断。

        evalscope 的 run_perf_benchmark 是同步阻塞调用，没有外部取消 API。
        将其放在子线程中执行，主线程轮询 _stop_event：
        - 如果 _stop_event 被设置，不再等待子线程返回，抛出 StopRequested 退出循环。
        - evalscope 内部使用全局 asyncio.Event，多线程需串行化。
        - 每次调用创建新事件循环并正确关闭，防止资源泄漏。

        ``sub_total`` 是本轮应发请求数，用于登记请求级进度：必须在 ``_target``
        线程内登记，因为 ``insert_benchmark_data`` 也在这个线程里执行，靠线程 id 归属。
        """
        import concurrent.futures

        result_box: list = []
        exc_box: list = []

        def _target():
            with self._evalscope_lock:
                _sub_register(sub_total)
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    ret = run_perf_benchmark(args)
                    result_box.append(ret)
                except Exception as e:  # noqa: BLE001
                    exc_box.append(e)
                finally:
                    _sub_unregister()
                    try:
                        pending = asyncio.all_tasks(loop)
                        for task in pending:
                            task.cancel()
                        if pending:
                            loop.run_until_complete(
                                asyncio.gather(*pending, return_exceptions=True)
                            )
                    except Exception:  # noqa: BLE001
                        pass
                    finally:
                        loop.close()

        t = threading.Thread(target=_target, daemon=True)
        t.start()
        # 轮询等待，同时检查停止信号
        while t.is_alive():
            t.join(timeout=1.0)
            if self._stop_event.is_set():
                raise StopRequested("用户请求停止")

        if exc_box:
            raise exc_box[0]
        if not result_box:
            raise StopRequested("用户请求停止")
        return result_box[0]

    def _build_arguments(self, cfg: RunConfig, task_type: str, io: IOGroup, cr: CRGroup,
                         api_key: str = "", outputs_dir: Path | None = None) -> Arguments:
        # 1) 工具内置默认值
        extra = {"ignore_eos": True}
        extra.update(cfg.evalscope_args.get("extra_args") or {})
        common = dict(
            api=cfg.api or "openai",
            read_timeout=600,
            connect_timeout=30,
            extra_args=extra,
            enable_progress_tracker=True,
            debug=False,
            log_every_n_query=cfg.log_every_n_query,
            prefix_length=cfg.prefix_length,
            tokenizer_path=cfg.tokenizer_path or None,
            outputs_dir=str(outputs_dir or (self._run_dir / "outputs")),
            stream=cfg.stream,
        )
        # 2) 高级面板 / 命令解析传入的 evalscope 原生参数（覆盖内置默认值）
        common.update(cfg.evalscope_args)
        # extra_args 做并集：保留 ignore_eos，同时允许用户追加/覆盖
        merged_extra = dict(extra)
        merged_extra.update(cfg.evalscope_args.get("extra_args") or {})
        # tools / tool_choice：走 extra_args 注入请求体（anthropic 插件负责转格式）
        if cfg.tools:
            merged_extra["tools"] = cfg.tools
            if cfg.tool_choice:
                merged_extra["tool_choice"] = cfg.tool_choice
        common["extra_args"] = merged_extra
        # anthropic 端点先归一化，避免 evalscope 的 openai 补全逻辑或用户漏写 /v1/messages
        api_url = cfg.api_url
        if (cfg.api or "").lower().startswith("anthropic"):
            try:
                from perf_anthropic import normalize_url
                api_url = normalize_url(api_url)
            except Exception:  # noqa: BLE001
                pass

        # 3) 批量网格强制接管（每个 sub-test 变化的维度）
        common.update(
            api=cfg.api or "openai",
            model=cfg.model_name,
            url=api_url,
            api_key=api_key or None,
            parallel=cr.parallel,
            number=cr.number,
            max_tokens=io.output if io.output > 0 else None,
        )
        # 4) 多轮模式（evalscope 走 run_multi_turn_benchmark 分支）
        if cfg.multi_turn:
            common["api"] = cfg.api
            common["multi_turn"] = True
            common["min_turns"] = cfg.min_turns
            common["max_turns"] = cfg.max_turns
            mt_kwargs: dict = {}
            if cfg.first_turn_length > 0:
                mt_kwargs["first_turn_length"] = cfg.first_turn_length
            if cfg.subsequent_turn_length > 0:
                mt_kwargs["subsequent_turn_length"] = cfg.subsequent_turn_length
            if mt_kwargs:
                common["multi_turn_args"] = MultiTurnArgs(**mt_kwargs)

        if task_type == "random":
            return Arguments(
                **common,
                dataset="random_multi_turn" if cfg.multi_turn else "random",
                min_prompt_length=io.input,
                max_prompt_length=io.input,
            )
        if task_type == "manual":
            # 复用 line_by_line 插件读取落盘的问题文件；每行是一个 JSON 字符串
            return Arguments(
                **common,
                dataset="line_by_line",
                dataset_path=str(self._manual_dataset_path),
            )
        # openclaw / 自定义 dataset
        common["dataset"] = cfg.openclaw_dataset_name
        if cfg.openclaw_dataset_path:
            common["dataset_path"] = cfg.openclaw_dataset_path
        # io.input = 0 表示不限制：不传 min/max_prompt_length，用 evalscope 默认（0 ~ 131072）
        if io.input > 0:
            common["min_prompt_length"] = io.input
            common["max_prompt_length"] = io.input
        return Arguments(**common)

    def _find_db_rel(self, sub_dir: Path) -> str:
        """在 sub-test 输出目录下定位 evalscope 落盘的 benchmark_data.db（相对 history 的路径）。

        evalscope 会在 outputs_dir 之下再拼 run_id / model / task / parallel_x_number_y，
        因此需要递归查找，取最新写入的那一个。
        """
        try:
            dbs = sorted(sub_dir.rglob("benchmark_data.db"), key=lambda p: p.stat().st_mtime)
        except OSError:
            dbs = []
        if not dbs:
            return ""
        try:
            return dbs[-1].relative_to(self.history_root).as_posix()
        except ValueError:
            return ""

    def _worker(self, cfg: RunConfig) -> None:
        writer = CsvWriter(self._csv_path, cfg.model_name)
        total = self._progress["total"]
        num_keys = len(cfg.openai_api_keys)
        _patch_evalscope_db()
        self._broadcast(f"=== 开始批量测试 @ {self._started_at} 共 {total} 个测试 ({num_keys} 个用户并发) ===")
        self._broadcast(f"=== 结果文件: {self._csv_path} ===")
        self._broadcast(
            f"=== 协议: {cfg.api} | 模式: "
            + (f"多轮对话 {cfg.min_turns}~{cfg.max_turns} 轮" if cfg.multi_turn else "单轮")
            + (f" | 首轮长度 {cfg.first_turn_length} / 后续增量 {cfg.subsequent_turn_length}"
               if cfg.multi_turn and (cfg.first_turn_length or cfg.subsequent_turn_length) else "")
            + (f" | tools {len(cfg.tools)} 个"
               + (f" (tool_choice={cfg.tool_choice})" if cfg.tool_choice else "")
               if cfg.tools else "")
            + " ==="
        )
        if cfg.evalscope_args:
            self._broadcast(
                "=== 附加 evalscope 参数: "
                + json.dumps(cfg.evalscope_args, ensure_ascii=False)
                + " ==="
            )

        try:
            if num_keys <= 1:
                # 单 Key：保持原有行为，无需多线程
                key = cfg.openai_api_keys[0] if cfg.openai_api_keys else ""
                self._run_key_loop(cfg, writer, key, key_idx=0, key_label="用户1")
            else:
                # 多 Key：每个 Key 启动独立线程并行跑全部子测试
                with ThreadPoolExecutor(max_workers=num_keys) as executor:
                    futures = {}
                    for i, api_key in enumerate(cfg.openai_api_keys):
                        key_label = f"用户{i + 1}"
                        f = executor.submit(self._run_key_loop, cfg, writer, api_key,
                                            key_idx=i, key_label=key_label)
                        futures[f] = key_label
                    for f in as_completed(futures):
                        label = futures[f]
                        try:
                            f.result()
                        except Exception as e:  # noqa: BLE001
                            self._broadcast(f"[error] {label} 线程异常: {e}")

            self._state = "completed"
            self._broadcast(f"=== 全部完成 @ {datetime.now().isoformat(timespec='seconds')} ===")
            self._broadcast(f"=== CSV: {self._csv_path} ===")
        except Exception as e:  # noqa: BLE001
            self._state = "error"
            self._error = str(e)
            self._broadcast(f"[fatal] worker 异常: {e}")
            self._broadcast(traceback.format_exc())
        finally:
            with _SUB_TRACK_LOCK:
                _SUB_TRACK.clear()
            self._detach_logger()

    def _run_key_loop(self, cfg: RunConfig, writer: CsvWriter,
                      api_key: str, key_idx: int, key_label: str) -> None:
        """单个 API Key 的完整子测试循环。多 Key 时由 ThreadPoolExecutor 并发调用。"""
        try:
            for task_type in cfg.task_types:
                self._broadcast(f"[{key_label}] --- 任务类型: {task_type} ---")
                for io in cfg.input_output_groups:
                    for cr in cfg.concurrency_request_groups:
                        run_results = []
                        for repeat in range(1, cfg.test_repeats + 1):
                            if self._stop_event.is_set():
                                self._broadcast(f"[{key_label}] 收到停止信号，退出")
                                with self._progress_lock:
                                    self._state = "stopped"
                                return

                            with self._progress_lock:
                                self._completed_count += 1
                                counter = self._completed_count
                            self._progress.update({
                                "current": counter,
                                "task_type": task_type,
                                "io": f"输入{io.input}/输出{io.output}",
                                "concurrency": f"并发{cr.parallel}",
                                "repeat": f"{repeat}/{cfg.test_repeats}",
                            })
                            self._broadcast(
                                f"=== [{key_label}] 测试 {counter}/{self._progress['total']}: "
                                f"task={task_type} repeat={repeat}/{cfg.test_repeats} "
                                f"input={io.input} output={io.output} "
                                f"parallel={cr.parallel} number={cr.number} ==="
                            )

                            sub_dir = self._run_dir / "outputs" / _sub_key(
                                task_type, io, cr, repeat, key_idx
                            )
                            try:
                                args = self._build_arguments(
                                    cfg, task_type, io, cr, api_key=api_key, outputs_dir=sub_dir,
                                )
                                # 请求级进度登记在 _target 线程内（与 insert_benchmark_data 同线程）
                                results = self._run_perf_benchmark_safe(args, sub_total=cr.number)
                            except StopRequested:
                                self._broadcast(f"[{key_label}] 收到停止信号，退出")
                                with self._progress_lock:
                                    self._state = "stopped"
                                return
                            except Exception as e:  # noqa: BLE001
                                self._broadcast(f"[{key_label}] [error] evalscope 调用失败: {e}")
                                self._broadcast(traceback.format_exc())
                                continue

                            key = f"parallel_{cr.parallel}_number_{cr.number}"
                            run = results.get(key) or next(iter(results.values()), None)
                            if not run or "metrics" not in run:
                                self._broadcast(f"[{key_label}] [warn] 未拿到 {key} 的结果，跳过 CSV")
                                continue

                            try:
                                row = writer.write_run(
                                    task_type=task_type,
                                    repeat_num=repeat,
                                    prompt_length=io.input,
                                    max_tokens=io.output,
                                    metrics=run["metrics"],
                                    percentiles=run["percentiles"],
                                    output_dir=str(sub_dir),
                                    api_key_index=key_idx + 1,
                                )
                                # 前端「请求详情」用：相对 history 的 db 路径
                                row["db"] = self._find_db_rel(sub_dir)
                                run_results.append(run)
                                self._broadcast_result(row)
                                self._broadcast(
                                    f"[{key_label}] [ok] {key}: TTFT={row['TTFT_Avg(s)']}s "
                                    f"TPOT={row['TPOT_Avg(s)']}s "
                                    f"OutThr={row['Output_through(tok/s)']}tok/s"
                                )
                            except Exception as e:  # noqa: BLE001
                                self._broadcast(f"[{key_label}] [error] 写 CSV 失败: {e}")

                            if counter < self._progress["total"] and cfg.wait_between_tests > 0:
                                self._broadcast(f"[{key_label}] --- 等待 {cfg.wait_between_tests}s 进入下一测试 ---")
                                self._sleep_with_stop(cfg.wait_between_tests)

                        # 一组 cr 的所有 repeat 跑完，算 AVG
                        if cfg.test_repeats > 1 and run_results:
                            try:
                                avg_row = writer.write_avg(
                                    task_type=task_type,
                                    prompt_length=io.input,
                                    max_tokens=io.output,
                                    runs=run_results,
                                    api_key_index=key_idx + 1,
                                )
                                if avg_row:
                                    self._broadcast_result(avg_row)
                                self._broadcast(f"[{key_label}] [avg] 写入 {len(run_results)} 次重复的平均值")
                            except Exception as e:  # noqa: BLE001
                                self._broadcast(f"[{key_label}] [error] 写 AVG 失败: {e}")
        except StopRequested:
            self._broadcast(f"[{key_label}] 收到停止信号，退出")
            with self._progress_lock:
                self._state = "stopped"
