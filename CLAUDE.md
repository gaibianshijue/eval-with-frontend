# CLAUDE.md — 项目开发指引

## 项目概述

基于 evalscope Python SDK 的批量压测 Web 工具。浏览器配置参数 → 启动 → 实时看日志和进度 → 一键停止 → 结果表格实时展示 + CSV 下载。

## 架构

```
浏览器 ──► Flask (app.py) ──► BenchmarkRunner (runner.py, 后台线程)
                                  │
                                  ├─► evalscope.perf.main.run_perf_benchmark(Arguments)
                                  │       └─► HTTP 直接打 OpenAI 兼容 API
                                  │
                                  ├─► CsvWriter (csv_writer.py) → history/<ts>/results_*.csv
                                  └─► SSELogHandler → evalscope logger → 浏览器 SSE
```

## 关键文件

| 文件 | 职责 |
|---|---|
| `app.py` | Flask 路由层：`/start`、`/stop`、`/status`、`/state`、`/stream`(SSE)、`/results`、`/download/csv`、`/evalscope/schema`、`/evalscope/parse-command`、`/db/*`（请求详情） |
| `runner.py` | 核心业务：`RunConfig` 配置校验、`BenchmarkRunner` 单例管理压测生命周期、结果广播；`evalscope_args` 透传其余 `Arguments` 字段；`_patch_evalscope_db()` 给结果库补列，`_sub_key()` / `_find_db_rel()` 管理每个 sub-test 的独立输出目录；`_SUB_TRACK` / `_sub_tick()` 按**线程 id** 统计当前 sub-test 的请求级进度（`insert_benchmark_data` 与 `run_perf_benchmark` 同线程，所以能在落库回调里精确归位） |
| `request_store.py` | 只读解析 `benchmark_data.db`：`list_databases` / `query`（响应码 + 关键字筛选）/ `detail`（请求体 + 解码响应）；`_SafeUnpickler` 限制反序列化类型，兼容 OpenAI / Anthropic 两种事件流；`detail` 额外返回 `response_headers`（老库无该列时返回 `{}`） |
| `evalscope_schema.py` | 反射 `Arguments` 生成参数元信息（供前端动态渲染高级面板）；解析 `evalscope perf ...` 命令回填表单；维护 `MANAGED_FIELDS` / `DATASET_PRESETS` / `MULTI_TURN_DATASETS` |
| `perf_anthropic.py` | Anthropic `/v1/messages` 压测插件，`@register_api('anthropic')`；负责请求体转换（system 抽离、tool_call → tool_use/tool_result）、`x-api-key` 鉴权、SSE 事件解析与 usage 统计；`GATEWAY_MAX_RETRIES=0` **默认不重试**（网关 400/503、连接被拒/EOF 都如实计入失败，才能反映全链路稳定性），调大才走指数退避，参数类 4xx 永不重试；`_post_once` 把 HTTP 响应头挂到 `BenchmarkData.response_headers`（dataclass 无 slots，可动态加属性），成功与失败响应都记 |
| `perf_datasets.py` | 数据集插件：`mock_messages`（`@register_dataset`，每行一个 messages 数组，整段历史作为**一次请求**发出）+ 同名覆盖 `random_multi_turn` 的 `SanitizedRandomMultiTurnDatasetPlugin`（清洗随机 token 解码出的 `Cc` 控制字符与 `Cf` 零宽字符，避免 `\u0000` 让网关截断 body） |
| `mock_dataset.py` | 转换脚本：把真实抓取的请求体（anthropic / openai 形态）转成 `datasets/*.jsonl` + `manifest.json`；`build_trace_raw` 产出**保真**快照（`tool_use`/`tool_result` 原样，剔除无 signature 的 `thinking`），`build_trace` 产出文本渲染版 |
| `csv_writer.py` | CSV 输出：23 列表头（尾部 `Avg_Turns`），`write_run`/`write_avg`，注意 `avg_ttft`/`avg_tpot`/`avg_itl` 单位是 ms，CSV 统一转 s |
| `templates/index.html` | 单页前端骨架（命令导入 / 连接 / 数据集（含工具定义）/ 批量网格 / 执行参数 / 高级参数 + 右侧进度日志 + 结果表 + 请求详情） |
| `static/app.css` `static/core.js` `static/run.js` `static/advanced.js` | 样式、表单核心（含 `tools` 校验）、运行态（日志/结果/SSE/进度/请求详情弹层）、高级参数面板与命令解析 |

## 开发约定

- **后端语言**: Python 3.12+，类型注解用 `from __future__ import annotations`
- **前端**: 原生 HTML/CSS/JS，无框架，无构建步骤
- **数据流**: 结构化结果通过 SSE `__RESULT__:` 前缀推送 JSON，前端解析后渲染表格
- **停止机制**: `run_perf_benchmark` 是同步阻塞调用，在子线程中执行，主线程轮询 `_stop_event`，检测到停止信号抛出 `StopRequested`
- **多 Key 并发**: 通过 `ThreadPoolExecutor` 并行，`_evalscope_lock` 串行化 evalscope 调用（避免全局 asyncio.Event 冲突）
- **CSV 单位**: `avg_ttft`/`avg_tpot`/`avg_itl` 从 evalscope 取出是 ms，CSV 和前端统一用 s，`_ms_to_s()` 函数做转换
- **API Key 脱敏**: `RunConfig.to_safe_dict()` 落盘时只保留 `sk-***XXXX`

## 路由

| 路由 | 方法 | 用途 |
|---|---|---|
| `GET /` | — | 单页 UI |
| `POST /start` | JSON | 启动压测 |
| `POST /stop` | — | 请求停止 |
| `GET /status` | JSON | 进度（`_current`/`_total` 任务数 + `_sub_*` 当前 sub-test 请求数 + task/IO/并发/ETA） |
| `GET /state` | JSON | running/state/run_dir/csv_path |
| `GET /stream` | SSE | 实时日志流 |
| `GET /results` | JSON | 累积结果行列表 |
| `GET /download/csv` | File | 下载 CSV |
| `GET /evalscope/schema` | JSON | `Arguments` 元信息 + `apis` + `dataset_presets` + `multi_turn_datasets` |
| `POST /evalscope/parse-command` | JSON | 解析 `evalscope perf ...` 命令 → 表单配置 |
| `GET /db/runs` | JSON | 列出所有 `benchmark_data.db`（条数 / 成功失败 / 大小 / mtime） |
| `GET /db/requests` | JSON | 分页查询请求记录，`db` + `status`（响应码/`failed`/`4xx`/`5xx`）+ `q` + `page` + `size` |
| `GET /db/request` | JSON | 单条详情：请求体、文本、思考内容、`tool_calls`、原始事件 |

## 运行

```bash
pip install -r requirements.txt
python app.py  # 监听 0.0.0.0:5000
```

## 注意事项

- `run_perf_benchmark` 内部使用模块级 `asyncio.Event`，多线程并发需串行化（`_evalscope_lock`）
- `random` 任务必须提供 `tokenizer_path`，`RunConfig.validate()` 做了前置校验
- `manual` 任务：前端录入 `{question, answer}` 列表（question 必填、answer 选填仅记录），`start()` 时把问题逐行 JSON 编码落盘到 `history/<ts>/manual_dataset.jsonl`，复用 evalscope `line_by_line` 插件读取（每行一个 JSON 字符串，可含换行）
- `stream` 参数（流式/非流式）经 `RunConfig.stream` 透传给 `Arguments.stream`，默认 True
- evalscope 的 `avg_ttft`/`avg_tpot`/`avg_itl` 单位是 ms，P90 的 `ttft`/`tpot` 也是 ms，`_p90_seconds` 已处理转换
- **anthropic 协议**：evalscope perf 无原生 Anthropic 插件，`runner.py` 顶部 `import perf_anthropic` 触发注册；
  插件会改写 `param.headers`（删 `Authorization`、加 `x-api-key` / `anthropic-version`）与 `param.url`（补 `/v1/messages`），
  因此凡是自建 `Arguments` 的地方都必须确保插件先于 HTTP 客户端构造
- **多轮模式**：`RunConfig.multi_turn` 为真时 `random` 任务改用 `random_multi_turn`（需要 tokenizer），
  `manual` 直接拒绝；`max_turns` 同时作为数据集侧截断上限；`multi_turn_args` 仅在 `first/subsequent_turn_length > 0` 时构造
- **tools / tool_choice**：`RunConfig.tools` 统一按 OpenAI 形状输入，经 `extra_args` 合并进请求体；
  anthropic 协议由 `perf_anthropic.convert_tools` / `convert_tool_choice` 转成 `input_schema` / `{type:auto|any|tool}`。
  不要在前端分叉两套格式
- **tool_call 结果核对**：evalscope 不解析 `tool_calls`，由 `request_store.decode_response()` 从
  `response_messages` 里还原（OpenAI `delta.tool_calls` 分片拼接 / Anthropic `content_block_start.tool_use` +
  `input_json_delta.partial_json`，两者都以事件 `index` 作为合并键）
- **IO 组 0 / 留空 = 不限制**：`IOGroup.input/output` 为 0 时 `_build_arguments` 不传 `min_prompt_length` /
  `max_prompt_length`，`max_tokens` 传 `None`（evalscope 的 `openai_api.py` 用 `if param.max_tokens is not None` 判断，
  不写字段）。`from_dict` 对空 `input_output_groups` 兜底 `[IOGroup(0, 0)]`，`validate` 只对 `random` 强制输入长度；
  前端 `collectConfig` 把空串记 0、`applyBuiltinDataset` 选中数据集时清空两列。CSV 里这两个值会是 0（表示不限）
- **`random_multi_turn` 的长度语义**：每条对话生成 `max_turns` 条 user 消息，每条长度 = `prefix_length + input`
  （`np.random.randint(min_prompt_length, max_prompt_length+1)`，runner 下两者都等于 input）；请求时 context 累积，
  第 n 轮 prompt ≈ `n×input + (n-1)×完成长度`。不是一条 `input×turns` 的长输入
- **random 可不开输出限制**：`IOGroup.output=0` → `max_tokens=None`，`openai_api.py` 用
  `if param.max_tokens is not None` 判断，请求体不写该字段（实测 3 轮 msgs=1/3/5 且无 max_tokens）；
  只有 `input` 对 random 必填
- **多轮数据集必须开 multi_turn**：多轮插件 `build_messages()` 产出 `List[Turn]`（dataclass），单轮
  `run_perf_benchmark` 的 `build_request` 会按 dict 取 `msg['role']` 而崩；只有 `args.multi_turn=True` 才进
  `run_multi_turn_benchmark`。runner 与前端 `isMultiTurnDataset()` 双向拦截，`MULTI_TURN_DATASETS` 是唯一名单
- **`max_turns` 是硬上限、`min_turns` 基本无效**：`custom.py` 在数据集层 `messages_per_turn[:max_turns]` 截断，
  `MultiTurnStrategy` 里 `turn_idx >= max_turns` 也 break；`min_turns` 只被 `random_multi_turn` 用于
  `np.random.randint(min, max+1)` 采样。前端选内置多轮数据集自动填 `max_turns = min(轮数, 10)`（22 轮数据集默认只跑 10 轮）
- **压测 400 先判别是不是上游故障**：网关会把后端失败统一包成
  `400 {"code":3,"msg":"invalid argument: http post fail: Post \"http://<内网LLM>/v1/chat/completions\": EOF"}`，
  与请求参数无关。判定 `msg` 含 `http post fail` / `connection refused` / `EOF` / `reset` / `502` 即为瞬时故障。
  批量失败集中在相邻 `start_time` + 后续全是 `connection refused` = 推理服务崩溃重启。
  长上下文（多轮第 40 轮左右、body ≈200KB / ≈45K tok、10 并发）是典型触发条件
- **connect_test 的 404 与数据集无关**：`test_connection` 发的是单条 `hello`
  （`messages=[{role:user,content:'hello'}]`），且它排在数据集加载**之后**，所以日志里的
  `Elapsed: 01:05` 主要是 `random_multi_turn` 生成对话的耗时（~2.2s/对话），不是超时。
  4xx 会被 `test_connection` 判为「不可重试」立即 bail 并抛 `TimeoutError('Test connection failed')`。
  404 只会是 URL 路径 / model 名 / api 协议错；403 是 key 失效（换任何 model 都 403）。
  `.../compatible-mode/v1/messages` 这类网关常只实现 OpenAI `/v1/chat/completions`
- **非 200 响应只能读一次 body**：先 `response.json()` 失败再 `response.text()` 会拿到空串，
  导致 error 为空、日志变成 `Non-retryable error (HTTP 404): .` 无法定位。现在统一先
  `raw = await response.text()`，再尝试 `json.loads` 美化，空 body 补
  `HTTP 404 NOT FOUND (empty response body)`
- **random prompt 必须清洗不可见字符**：`random_multi_turn` 用随机 token id 解码，会产出 `Cc` 控制字符
  （`\u0000`–`\u001f`、`\u007f`）和 `Cf` 零宽字符（U+200B/FEFF/00AD/202C）。`\u0000` 在部分网关会被当字符串
  终止符导致 body 截断 → `400 / EOF`。`perf_datasets.py` 的 `SanitizedRandomMultiTurnDatasetPlugin` 用同名覆盖
  注册并清洗；**注册要发生在 evalscope 插件 import 之后**（`register_dataset` 后注册覆盖先注册）
- **mock 数据集**：`datasets/*.jsonl` 由 `mock_dataset.py` 从真实请求体生成，`manifest.json` 是前端下拉的唯一数据源
  （`GET /datasets`）。`mock_messages` 一行 = 一次完整历史请求（重放轨迹），`custom_multi_turn` 一行 = 一段对话、
  由 runner 逐轮累加真实回复。两者不要混用：前者的 `number` 是请求数，后者的 `number` 是对话数。
  源会话 22 次提问却带 346 次工具往返，所以多轮数据集只保留 22 轮、工具结果内联，避免 `max_turns` 被误解成 200+
- **带真实工具块的请求只能走 `mock_messages` 单轮重放，不能走多轮**：`custom_multi_turn` 的 `_split_into_turns`
  把 `assistant` 当轮次边界后**丢弃 content**，`tool_use` 随之消失、`role=tool` 变成孤立 `tool_result`，
  第 2 轮必被 Anthropic 判 `400 unexpected tool_use_id found in tool_result blocks`（已实测）。
  同理 `MultiTurnStrategy` 回填上下文只写 `{'role':'assistant','content':generated_text}`，
  模型自己返回的 `tool_use` 不会进历史，多轮下工具往返语义必然丢失
- **保真快照必须剔 `thinking`**：抓包 320 个 `thinking` 块全部无 `signature`，原样回传真实 Anthropic 会报
  `thinking blocks must include signature`。剔除后 346/346 工具配对完整、孤立 0、735 消息 / 516KB / 73K token
- **结果库扩展列**：`_patch_evalscope_db()` 替换 `metrics_consumer.create_result_table` /
  `insert_benchmark_data`，在 evalscope 原生逻辑之后 `ALTER TABLE` / `UPDATE` 补
  `status_code` / `error` / `trace_id` / `turns` / `generated_text`；读取侧用 `PRAGMA table_info` 兼容老库
- **sub-test 输出目录**：evalscope 会在 `outputs_dir` 下再拼 `run_id/model/task/parallel_x_number_y`，
  多个 sub-test 共用一个目录会互相覆盖 `benchmark_data.db`，因此每个 sub-test 用独立 `_sub_key()` 子目录，
  落库路径用 `_find_db_rel()` 递归定位（不要手拼路径）
