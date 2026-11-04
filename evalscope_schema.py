"""evalscope perf 参数元信息 + `evalscope perf ...` 命令行解析。

两个能力：
1. `describe_arguments()` —— 从 `evalscope.perf.arguments.Arguments` 反射出全部字段
   （类型 / 默认值 / 帮助文本 / 对应 CLI flag / UI 分组），前端据此自动渲染「高级参数」面板，
   保证 evalscope 升级新增字段时 UI 自动跟上。
2. `parse_perf_command()` —— 直接解析用户粘贴的 `evalscope perf --model ... --url ...` 命令，
   映射回本工具的批量表单配置（连接参数 / 批量网格 / 高级透传参数）。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import shlex
import typing
from typing import Any, Optional

from evalscope.perf.arguments import Arguments, add_argument

# ---------------------------------------------------------------- UI 分组
# 字段名 → 分组。未列出的字段自动归入「其他参数」。
FIELD_GROUPS: list[tuple[str, str]] = [
    (
        "connection",
        "连接与鉴权",
        [
            "model", "model_id", "api", "url", "api_key", "headers",
            "tokenizer_path", "attn_implementation", "port",
            "connect_timeout", "read_timeout", "total_timeout", "no_test_connection",
        ],
    ),
    (
        "load",
        "压力与并发",
        [
            "number", "parallel", "rate", "warmup_num", "open_loop",
            "sleep_interval", "duration",
            "queue_size_multiplier", "in_flight_task_multiplier", "db_commit_interval",
        ],
    ),
    (
        "sla",
        "SLA 自动调优",
        [
            "sla_auto_tune", "sla_variable", "sla_params", "sla_num_runs",
            "sla_upper_bound", "sla_lower_bound", "sla_fixed_parallel", "sla_number_multiplier",
        ],
    ),
    (
        "dataset",
        "数据集与提示词",
        [
            "dataset", "dataset_path", "dataset_offset", "prompt", "query_template",
            "apply_chat_template", "max_prompt_length", "min_prompt_length", "prefix_length",
            "image_width", "image_height", "image_format", "image_num", "image_patch_size",
        ],
    ),
    (
        "response",
        "采样与响应",
        [
            "max_tokens", "min_tokens", "temperature", "top_p", "top_k", "seed",
            "frequency_penalty", "repetition_penalty", "n_choices", "stop", "stop_token_ids",
            "logprobs", "stream", "extra_args", "tokenize_prompt",
        ],
    ),
    (
        "multiturn",
        "多轮对话",
        ["multi_turn", "min_turns", "max_turns", "multi_turn_args"],
    ),
    (
        "output",
        "日志与输出",
        [
            "outputs_dir", "no_timestamp", "name", "log_every_n_query", "debug",
            "enable_progress_tracker", "visualizer", "wandb_api_key", "swanlab_api_key",
            "swanlab_host",
        ],
    ),
]

# 这些字段由本工具的批量网格接管，前端高级面板里只读展示（不作为透传参数提交）
MANAGED_FIELDS = {
    "model", "url", "api_key", "parallel", "number", "max_tokens",
    "dataset", "dataset_path", "min_prompt_length", "max_prompt_length",
    "outputs_dir", "tokenizer_path", "stream",
    "prefix_length", "log_every_n_query", "enable_progress_tracker",
    # 协议与多轮由独立控件接管
    "api", "multi_turn", "min_turns", "max_turns", "multi_turn_args",
}

#: evalscope 的多轮数据集（走 run_multi_turn_benchmark 分支）
MULTI_TURN_DATASETS = {
    "random_multi_turn",
    "share_gpt_zh_multi_turn",
    "share_gpt_en_multi_turn",
    "custom_multi_turn",
    "swe_smith",
    "trie_agentic_coding",
    "trie_code_qa",
    "trie_office_work",
}

#: 必须提供本地路径的多轮数据集
MULTI_TURN_PATH_REQUIRED = {"custom_multi_turn"}

#: 前端「数据集」下拉预设（group 仅用于分组展示）
DATASET_PRESETS: list[dict] = [
    {"name": "random", "label": "random（随机 token，需 tokenizer）", "group": "单轮", "needs_path": False},
    {"name": "line_by_line", "label": "line_by_line（每行一个问题）", "group": "单轮", "needs_path": True},
    {"name": "openqa", "label": "openqa（内置问答集）", "group": "单轮", "needs_path": False},
    {"name": "longalpaca", "label": "longalpaca（长上下文）", "group": "单轮", "needs_path": False},
    {"name": "flickr8k", "label": "flickr8k（多模态）", "group": "单轮", "needs_path": False},
    {"name": "custom", "label": "custom（自定义 JSONL）", "group": "单轮", "needs_path": True},
    {"name": "mock_messages", "label": "mock_messages（完整 messages 快照重放）", "group": "单轮", "needs_path": True},
    {"name": "random_multi_turn", "label": "random_multi_turn（合成多轮，需 tokenizer）", "group": "多轮", "needs_path": False},
    {"name": "share_gpt_zh_multi_turn", "label": "share_gpt_zh_multi_turn（中文多轮）", "group": "多轮", "needs_path": False},
    {"name": "share_gpt_en_multi_turn", "label": "share_gpt_en_multi_turn（英文多轮）", "group": "多轮", "needs_path": False},
    {"name": "custom_multi_turn", "label": "custom_multi_turn（自定义多轮 JSONL）", "group": "多轮", "needs_path": True},
    {"name": "swe_smith", "label": "swe_smith（agentic 工具轨迹）", "group": "Agentic", "needs_path": False},
    {"name": "trie_agentic_coding", "label": "trie_agentic_coding（编码 agent 轨迹）", "group": "Agentic", "needs_path": False},
    {"name": "trie_code_qa", "label": "trie_code_qa（代码问答轨迹）", "group": "Agentic", "needs_path": False},
    {"name": "trie_office_work", "label": "trie_office_work（办公 agent 轨迹）", "group": "Agentic", "needs_path": False},
]

# 计算属性 / 运行时注入，不进入 UI
HIDDEN_FIELDS = {"model_id", "func"}


def _cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evalscope perf", add_help=False)
    add_argument(parser)
    return parser


def _field_kind(annotation: Any) -> tuple[str, str]:
    """把 pydantic 注解粗略归成前端控件类型，返回 (kind, item_kind)。"""
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)

    if origin is typing.Union:
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _field_kind(non_none[0])
        # 形如 Union[List[float], float]（如 rate）按列表处理
        for a in non_none:
            if typing.get_origin(a) in (list, typing.List):
                return _field_kind(a)
        return "json", "json"
    if origin in (list, typing.List):
        inner, _ = _field_kind(args[0]) if args else ("text", "text")
        if inner in ("text", "int", "float", "bool"):
            return "list", inner
        return "json", "json"
    if origin in (dict, typing.Dict):
        return "json", "json"
    if annotation is bool:
        return "bool", "bool"
    if annotation is int:
        return "int", "int"
    if annotation is float:
        return "float", "float"
    if annotation is str:
        return "text", "text"
    return "json", "json"


def _default_json(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (list, tuple)):
        return [_default_json(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _default_json(x) for k, x in v.items()}
    return str(v)


def describe_arguments() -> dict:
    """返回全部 perf 参数元信息 + 分组，供前端动态渲染。"""
    parser = _cli_parser()
    flags: dict[str, list[str]] = {}
    helps: dict[str, str] = {}
    choices: dict[str, list] = {}
    for action in parser._actions:
        if action.dest in ("help", argparse.SUPPRESS):
            continue
        flags[action.dest] = [s for s in action.option_strings if s.startswith("--")] or action.option_strings
        if action.help and action.help is not argparse.SUPPRESS:
            helps[action.dest] = re.sub(r"\s+", " ", action.help).strip()
        if action.choices:
            choices[action.dest] = list(action.choices)

    grouped: dict[str, list[str]] = {}
    for key, _label, names in FIELD_GROUPS:
        for n in names:
            grouped[n] = key

    # 协议枚举（evalscope 用 register_api 注册，没有出现在 argparse choices 里）
    choices["api"] = api_names()

    fields: list[dict] = []
    for name, info in Arguments.model_fields.items():
        if name in HIDDEN_FIELDS:
            continue
        kind, item_kind = _field_kind(info.annotation)
        raw_default = info.default
        if info.is_required() or str(raw_default) == "PydanticUndefined":
            default = None
        else:
            default = _default_json(raw_default)
        fields.append({
            "name": name,
            "flag": (flags.get(name) or [f"--{name.replace('_', '-')}"])[0],
            "kind": kind,
            "item_kind": item_kind,
            "default": default,
            "required": bool(info.is_required()),
            "managed": name in MANAGED_FIELDS,
            "group": grouped.get(name, "other"),
            "help": helps.get(name, ""),
            "choices": choices.get(name),
        })

    groups = [{"key": "other", "label": "其他参数"}] + [
        {"key": key, "label": label} for key, label, _ in FIELD_GROUPS
    ]
    # 按分组顺序重排 fields
    order = {g["key"]: i for i, g in enumerate(groups)}
    fields.sort(key=lambda f: (order.get(f["group"], 99), f["name"]))
    return {
        "version": _evalscope_version(),
        "groups": groups,
        "fields": fields,
        "field_names": sorted(Arguments.model_fields.keys()),
        "managed_fields": sorted(MANAGED_FIELDS),
        "apis": api_names(),
        "dataset_presets": DATASET_PRESETS,
        "multi_turn_datasets": sorted(MULTI_TURN_DATASETS),
    }


def api_names() -> list[str]:
    """已注册的 API 插件名（含本工具补的 anthropic）。"""
    try:
        import perf_anthropic  # noqa: F401  触发注册
    except Exception:  # noqa: BLE001
        pass
    try:
        from evalscope.perf.plugin.registry import ApiRegistry
        return sorted(ApiRegistry.all_classes())
    except Exception:  # noqa: BLE001
        return ["openai"]


def _evalscope_version() -> str:
    try:
        import evalscope
        return getattr(evalscope, "__version__", "")
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------- 命令解析
_PREFIX_RE = re.compile(
    r"^\s*(?:\([^)]*\)\s*)?(?:python[\d.]*\s+-m\s+)?evalscope(?:\.exe)?\s+perf\b\s*", re.IGNORECASE
)


def _tokenize(command: str) -> list[str]:
    """按 shell 规则切词，但保留 Windows 反斜杠路径。

    posix=False 会保留引号与反斜杠，随后手工剥离成对引号。
    """
    command = command.replace("\r\n", "\n").strip()
    # 支持多行命令（行尾 \ 续行）
    command = re.sub(r"[\\^]\s*\n\s*", " ", command)
    command = " ".join(line.strip() for line in command.split("\n") if line.strip())
    try:
        tokens = shlex.split(command, posix=False)
    except ValueError as e:
        raise ValueError(f"命令分词失败: {e}") from e
    cleaned: list[str] = []
    for tok in tokens:
        if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
            tok = tok[1:-1]
        cleaned.append(tok)
    return cleaned


def _split_task_types(dataset: Optional[str]) -> tuple[list[str], list[str]]:
    """根据 dataset 推断本工具的 task_types，返回 (task_types, warnings)。"""
    warnings: list[str] = []
    ds = (dataset or "").strip()
    if not ds:
        return ["random"], warnings
    if ds in ("random", "random_multi_turn"):
        return ["random"], warnings
    if ds == "line_by_line":
        warnings.append("dataset=line_by_line 无法区分 openclaw 与 manual，默认按 openclaw 处理")
        return ["openclaw"], warnings
    return ["openclaw"], warnings


def parse_perf_command(command: str) -> dict:
    """解析 `evalscope perf ...` 命令，返回 {config, args, warnings, unknown}。

    解析失败抛 ValueError（由路由转成 400）。
    """
    raw = (command or "").strip()
    if not raw:
        raise ValueError("命令为空")

    body = _PREFIX_RE.sub("", raw, count=1)
    if body == raw:
        # 没写 evalscope perf 前缀，只要出现 --model 就当纯参数串处理
        if "--model" not in raw and "-m" not in raw:
            raise ValueError("未识别到 `evalscope perf` 命令，请粘贴形如 `evalscope perf --model ... --url ...` 的完整命令")

    tokens = _tokenize(body)
    parser = _cli_parser()
    cli_defaults = {
        a.dest: a.default for a in parser._actions if a.dest not in ("help", argparse.SUPPRESS)
    }

    err_buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(err_buf), contextlib.redirect_stdout(io.StringIO()):
            ns, unknown = parser.parse_known_args(tokens)
    except SystemExit as e:  # argparse 出错时退出
        msg = err_buf.getvalue().strip() or f"参数解析失败 (exit={e.code})"
        raise ValueError(msg) from None

    parsed = {k: v for k, v in vars(ns).items() if v is not None}
    # 命令行里显式写出的字段（与 argparse 默认值不同），用于回填高级参数
    explicit = {
        k for k, v in parsed.items()
        if k in Arguments.model_fields and v != cli_defaults.get(k)
    }

    # 用 Arguments 做一次归一化（list 化 number/parallel、url 补全等）
    normalized: dict = {}
    warnings: list[str] = []
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            args_obj = Arguments(**parsed)
        normalized = args_obj.model_dump(exclude_none=True)
    except Exception as e:  # noqa: BLE001
        warnings.append(f"参数组合未通过 evalscope 校验（仍按原值填充）: {e}")
        normalized = dict(parsed)

    model = str(normalized.get("model") or parsed.get("model") or "").strip()
    url = str(normalized.get("url") or parsed.get("url") or "").strip()
    dataset = str(normalized.get("dataset") or "openqa").strip()
    dataset_path = normalized.get("dataset_path")
    tokenizer_path = normalized.get("tokenizer_path") or ""

    # parallel / number → 批量网格（多值 sweep 直接展开为多行）
    parallel = normalized.get("parallel", parsed.get("parallel", 1))
    number = normalized.get("number", parsed.get("number", 1000))
    parallel_list = parallel if isinstance(parallel, list) else [parallel]
    number_list = number if isinstance(number, list) else [number]
    if len(parallel_list) != len(number_list):
        raise ValueError(
            f"--parallel 与 --number 数量不一致（{len(parallel_list)} vs {len(number_list)}），"
            "evalscope 要求一一对应"
        )
    cr_groups = [{"parallel": int(p), "number": int(n)} for p, n in zip(parallel_list, number_list)]

    max_tokens = normalized.get("max_tokens", parsed.get("max_tokens", 2048))
    if isinstance(max_tokens, list):
        warnings.append(f"--max-tokens 为区间 {max_tokens}，批量表单只取上限")
        max_tokens = max_tokens[-1]
    prompt_in = normalized.get("max_prompt_length", parsed.get("max_prompt_length", 0))
    min_prompt = normalized.get("min_prompt_length", parsed.get("min_prompt_length", 0))
    if min_prompt and prompt_in and min_prompt != prompt_in:
        warnings.append(
            f"--min-prompt-length({min_prompt}) 与 --max-prompt-length({prompt_in}) 不同，批量表单取 max"
        )
    io_groups = [{"input": int(prompt_in or 0), "output": int(max_tokens or 0)}]

    task_types, tt_warnings = _split_task_types(dataset)
    warnings.extend(tt_warnings)

    api_keys: list[str] = []
    api_key = normalized.get("api_key") or parsed.get("api_key")
    if api_key:
        api_keys = [k.strip() for k in str(api_key).split(",") if k.strip()]

    # 高级透传参数 = 命令行里显式写出、且不被批量表单接管的字段
    overrides = {}
    for k in sorted(explicit):
        if k in MANAGED_FIELDS:
            continue
        v = normalized.get(k, parsed.get(k))
        if v is None or v == "" or v == {} or v == []:
            continue
        overrides[k] = _default_json(v)

    # 协议 / 多轮：从命令行显式参数回填到批量表单顶层字段
    api = str(normalized.get("api") or "openai").strip() or "openai"
    multi_turn = bool(normalized.get("multi_turn", False)) or dataset in MULTI_TURN_DATASETS
    min_turns = int(normalized.get("min_turns") or 1)
    max_turns = int(normalized.get("max_turns") or (2 if multi_turn else 1))
    mt_args = normalized.get("multi_turn_args") or {}
    if isinstance(mt_args, str):
        try:
            mt_args = json.loads(mt_args)
        except json.JSONDecodeError:
            mt_args = {}
    if "multi_turn_args" not in explicit or not isinstance(mt_args, dict):
        mt_args = {}
    first_turn_length = int(mt_args.get("first_turn_length") or 0)
    subsequent_turn_length = int(mt_args.get("subsequent_turn_length") or 0)

    config = {
        "model_name": model,
        "api_url": url,
        "tokenizer_path": tokenizer_path,
        "openai_api_keys": api_keys,
        "openclaw_dataset_path": dataset_path or "",
        "openclaw_dataset_name": dataset,
        "stream": bool(normalized.get("stream", True)),
        "task_types": task_types,
        "api": api,
        "multi_turn": multi_turn,
        "min_turns": min_turns,
        "max_turns": max_turns,
        "first_turn_length": first_turn_length,
        "subsequent_turn_length": subsequent_turn_length,
        "input_output_groups": io_groups,
        "concurrency_request_groups": cr_groups,
        "evalscope_args": overrides,
    }

    return {
        "config": config,
        "warnings": warnings,
        "unknown_flags": unknown,
        "normalized": {k: _default_json(v) for k, v in normalized.items()},
        "command": raw,
    }
