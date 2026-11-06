# eval-with-frontend

带有前端的压测工具，基于evalscope sdk

把 evalscope 压测能力包装成 Web 服务：在浏览器配置参数 → 启动 → 实时看日志和进度 → 一键停止。

**v2 架构**：Flask → 直接调用 `evalscope` Python SDK（in-process），不再依赖 bash / jq / bc / 子进程。

## 启动方式

```bash
# 装依赖
pip install -r requirements.txt

# 启动
python app.py
# 浏览器打开 http://127.0.0.1:5000
```

Windows + conda base 示例：
```bash
conda activate base
pip install -r requirements.txt
python app.py
```

## 架构

```
浏览器 ──► Flask ──► BenchmarkRunner (后台线程)
                       │
                       ├─► evalscope.perf.main.run_perf_benchmark(Arguments)
                       │       └─► HTTP 直接打 OpenAI 兼容 API
                       │
                       ├─► CsvWriter   →  history/<ts>/results_*.csv
                       └─► SSELogHandler  →  evalscope logger → 浏览器 SSE
```

| 路由 | 用途 |
|---|---|
| `GET  /`       | 单页 UI |
| `POST /start`  | 启动一次任务（JSON 配置） |
| `POST /stop`   | 请求停止（当前 sub-test 跑完后退出） |
| `GET  /state`  | running / state / run_dir |
| `GET  /status` | 进度（current / total / 当前 task / IO / 并发 / 重复 / ETA） |
| `GET  /stream` | SSE 流式日志（含 `__RESULT__:` 结果行） |
| `GET  /results` | 已累积的结果行 |
| `GET  /evalscope/schema` | `Arguments` 全量参数元信息（类型 / 默认值 / CLI flag / 分组） |
| `POST /evalscope/parse-command` | 解析一条 `evalscope perf ...` 命令 → 表单配置 |
| `GET  /db/runs` | 列出 `history/**/outputs/**/benchmark_data.db`（含条数 / 成功失败 / 大小） |
| `GET  /db/requests` | 分页查询单次请求，支持 `status`（响应码）/ `q`（关键字）筛选 |
| `GET  /db/request` | 单条请求详情：请求体 + 解码后的响应（文本 / 思考 / tool_calls / 原始事件 / **响应头**） |
| `GET  /datasets` | 列出 `datasets/manifest.json` 登记的本地数据集（前端「内置数据集」下拉） |

### 进度条

一次任务会展开成 `task_type × IO × 并发 × 重复` 多个 sub-test，所以进度分两层：

| 位置 | 含义 | 字段 |
|---|---|---|
| 总进度条（粗） | **任务个数**进度：已启动 sub-test / 总 sub-test 数 | `_current` / `_total` / `_percent` |
| 子进度条（细） | **当前 sub-test 内的请求个数**进度：已完成请求 / 本轮应发请求数 | `_sub_current` / `_sub_total` / `_sub_percent` |

子进度是在 `insert_benchmark_data` 落库时按线程 id 归属计数的，口径与 evalscope 自带
tqdm 完全一致：warmup 不计、多轮只在 `is_last_turn` 计一次，所以 `_sub_total` 就是
`number`（多 key 并发时为各 key 汇总）。没有 sub-test 在跑时 `_sub_total` 为 0，子进度条归零显示 `--`。

### 响应头记录

每条请求的 HTTP 响应头会写进 `benchmark_data.db` 的 `response_headers` 列（json），
详情弹层新增「响应头」标签展示。排障时很有用：网关的 `x-request-id` / `x-ratelimit-*` /
上游耗时头只在响应头里，响应体里看不到。

- 仅 **Anthropic 协议**插件（`perf_anthropic.py`）记录；OpenAI 协议走 evalscope 原生插件，
  该列为空，前端提示「该记录没有响应头」
- **成功与失败都记**：非 200 响应的头对定位 400/503 更有价值
- 上限 40 个头、单值 512 字符，避免异常 header 撑爆 db
- 补列之前产生的历史记录不会有该字段（老库读取时返回 `{}`，不会报错）

### 流式结束判定（为什么最后 1~2 个请求会「卡住」）

**现象**：请求级进度停在 `63/64` 不动，等很久也不结束；点「停止」立刻恢复正常。

**原因**：Anthropic SSE 以 `message_stop` 事件终止，但代理型网关经常在它之后继续
keep-alive 而不立即关闭连接。插件最初只靠 `response.content.iter_any()` 读到 EOF 才结束，
于是 token 早就发完了，却还要空等到 `read_timeout`（默认 600s）才返回；如果网关期间
持续发 SSE 心跳注释，`sock_read` 永不触发，会一直卡到 `total_timeout`。

并发压测里是否命中取决于网关连接复用，所以通常只是**最后 1~2 个**请求撞上。

**修复**：流式分支识别到 `message_stop` 立刻跳出循环，并以该事件的到达时刻作为完成时间
（不把空等时间算进 latency）。

**注意**：

- OpenAI 协议走 evalscope 原生插件，同样以「`[DONE]` 后连接关闭」为结束信号；
  若该协议也出现同款卡顿，需要按同样思路包一层
- 网关既不发 `message_stop` 也不关连接时仍会落到 `read_timeout`，可用
  `evalscope_args` 把 `read_timeout` 调小来快速失败

实测（网关在 `message_stop` 后 hold 15s）：3 请求串行，修复前累计 45s+，修复后 1.0s，
`Avg_Latency` 不含空等时间。

## 配置项 → SDK Arguments 映射

| 前端字段 | `evalscope.perf.Arguments` |
|---|---|
| model_name | `model` |
| api_url | `url` |
| tokenizer_path | `tokenizer_path`（选择 `random` 任务时必填，等价于 evalscope `--tokenizer-path`） |
| openai_api_keys[] | `api_key`（每个 key 视为一个并行用户；仅传给 SDK，不落盘） |
| dataset_path | `dataset_path`（自定义数据集任务） |
| dataset_name | `dataset`（默认 `line_by_line`） |
| input_output_groups[].input | `min_prompt_length` / `max_prompt_length`（random 任务） |
| input_output_groups[].output | `max_tokens` |
| concurrency_request_groups[].parallel | `parallel` |
| concurrency_request_groups[].number | `number` |
| log_every_n_query | `log_every_n_query` |
| prefix_length | `prefix_length` |
| wait_between_tests | 各 sub-test 之间 sleep（runner 自管） |
| task_types | random / dataset / manual（runner 内分支处理；`random` 需要同时提供 `tokenizer_path`，`dataset` 即「自定义数据集」） |
| api | `api`（协议：`openai` / `anthropic` / `openai_responses` / `dashscope`；由「接口协议」下拉接管） |
| multi_turn | `multi_turn`（多轮对话压测开关） |
| min_turns / max_turns | `min_turns` / `max_turns`（多轮轮数范围；`max_turns` 同时是多轮数据集的截断上限） |
| first_turn_length / subsequent_turn_length | `multi_turn_args.first_turn_length` / `.subsequent_turn_length`（>0 才下发，0 = 用 evalscope 默认 65000 / 500） |
| tools[] | `extra_args.tools`（OpenAI 形状 `{"type":"function","function":{...}}`；anthropic 协议由插件转成 `input_schema`） |
| tool_choice | `extra_args.tool_choice`（`auto` / `none` / `required` / 具体函数名；anthropic 映射为 `auto` / `any` / `tool`） |
| evalscope_args{} | 其余 **全部** `Arguments` 字段（键名即字段名），如 `temperature` / `top_p` / `seed` / `rate` / `open_loop` / `sla_auto_tune` / `headers` / `extra_args` 等 |

内置默认值：`api` 取「接口协议」下拉，`extra_args={"ignore_eos": True}`，`read_timeout=600`，`connect_timeout=30`，`enable_progress_tracker=True`。
其中除 `enable_progress_tracker` 外都可通过 `evalscope_args` 覆盖（`extra_args` 与内置值做并集）。

## 协议支持

| api | 端点 | 说明 |
|---|---|---|
| `openai` | `/v1/chat/completions` | 默认，OpenAI 兼容（vLLM / SGLang / 各类网关） |
| `anthropic` | `/v1/messages` | **本工具内置适配插件** `perf_anthropic.py`，见下 |
| `openai_responses` | `/v1/responses` | evalscope 原生 |
| `dashscope` | DashScope 原生 | evalscope 原生 |

evalscope 的 perf 模块本身没有 Anthropic 插件（请求体与响应解析都按 OpenAI 形状写死），
`perf_anthropic.py` 补了一层适配：

- 请求：`system` 消息抽离为顶层 `system`，`messages` 归一化为 Anthropic block 格式，
  `role=tool` → `tool_result`、`assistant.tool_calls` → `tool_use`，`max_tokens` 缺省回退 1024，
  OpenAI 专属字段（`ignore_eos` / `stream_options` 等）自动剔除
- 鉴权：用 `x-api-key` + `anthropic-version: 2023-06-01`，并移除 evalscope 注入的 `Authorization`
- URL：填域名、`/v1`、`/v1/messages` 都会归一化为 `.../v1/messages`
- 响应：解析 `message_start` / `content_block_delta` / `message_delta`，
  token 数取 `usage.input_tokens` / `usage.output_tokens`（含 `cache_read_input_tokens`），TTFT 取首个文本增量
- 工具调用：`tools` 的 `parameters` 自动转 `input_schema`，`tool_choice` 映射为
  `{"type":"auto"|"any"}` 或 `{"type":"tool","name":"..."}`；响应中的 `tool_use`
  block 与 `input_json_delta` 会被完整还原

## 多轮对话与 tool_call

勾选「多轮对话压测」后走 evalscope 的 `run_multi_turn_benchmark` 分支：

- `random` 任务自动切换为 `random_multi_turn`（需要 `tokenizer_path`），每轮 prompt 长度仍取「输入长度」：
  - 数据集阶段：每条对话生成 `max_turns` 条 user 消息，**每条**长度从 `[输入长度, 输入长度]`（+ 高级参数 `prefix_length`）
    采样，所以是「N 条各 2K」，不是「一条 2K×N」
  - 请求阶段：多轮 strategy 把前 n-1 轮的 user + 模型真实回复拼进 context，所以第 n 轮请求的 prompt
    ≈ `n × 输入长度 + (n-1) × 实际回复长度`（输出留空时回复长度由服务端决定）
  - 例：输入 2000、`min_turns=max_turns=10` → 第 10 次请求的 prompt ≈ 20,000 tok（输出不计）
  - 前端多轮面板下方会按当前输入实时显示这套估算（`updateMultiTurnPreview`）
- `random` 任务的**输出可以留空**（0 / 空值 = 不传 `max_tokens`），只有输入长度必填；
  mock 实测请求体里 `max_tokens` 字段直接不存在，输出长度由服务端默认值决定
- `dataset` 任务可选多轮数据集：`random_multi_turn` / `share_gpt_zh_multi_turn` / `share_gpt_en_multi_turn` /
  `custom_multi_turn`（需路径）/ `swe_smith` / `trie_agentic_coding` / `trie_code_qa` / `trie_office_work`
- **选多轮数据集必须勾「多轮对话压测」**：这些插件产出的是 `List[Turn]`，只有 `args.multi_turn=True` 才走
  `run_multi_turn_benchmark`；不勾会退回单轮 `run_perf_benchmark`，`build_request` 拿到 `Turn` 直接报错。
  前端选内置多轮数据集时自动勾选，手动取消后提交会被前端和后端同时拦下
- 「请求数」在多轮下表示**对话数**，`min_turns`/`max_turns` 控制每个对话的轮数：
  - `max_turns` 是**硬上限**：`custom_multi_turn` 在数据集层 `messages_per_turn[:max_turns]` 截断，
    多轮 strategy 里 `turn_idx >= max_turns` 也会 break；必须 ≥ 1 且 ≥ `min_turns`
  - `min_turns` 只对 `random_multi_turn` 有意义（每条对话轮数在 `[min_turns, max_turns]` 均匀随机采样），
    `custom_multi_turn` 等真实数据集下被忽略
  - 内置 `mock_agent_multi_turn.jsonl` 共 22 轮，前端默认把 `max_turns` 填成 `min(22, 10)=10`（只跑前 10 轮）；
    想跑满整段就把「最大轮数」改成 22，快速冒烟就填 2~3
  - `first_turn_length` / `subsequent_turn_length` 只影响合成数据集（`random_multi_turn`），
    真实数据集自带文本，填了也不会改变上下文长度

- **工具定义下发**：勾选「下发工具定义（tools / function calling）」后填写 OpenAI 形状的 `tools` JSON，
  可选 `tool_choice`，两者经 `extra_args` 合并进请求体（OpenAI 协议原样下发，anthropic 协议由插件转格式）
- evalscope 原生不会下发 `tools` / 解析 `tool_calls`，因此请求详情页单独提供 `tool_calls` 标签页用于核对返回结果；
  `trie_*` / `swe_smith` 这类 agentic 数据集仍是把工具输出作为后续消息注入（可配置 `tool_call_latency` 模拟工具耗时），
  测的是长上下文 + 多轮时序。`custom_multi_turn` 的 `assistant` 消息只作为轮次边界且内容被丢弃，
  `role=tool` 消息会保留注入 —— 这会让 `tool_use` 与 `tool_result` 脱钩，**带真实工具块的数据集在多轮模式下第 2 轮就 400**，
  想压「含几百次工具调用的完整请求」请用 `mock_messages` 单轮重放（见「本地 mock 数据集」）。

### 启动时 connect_test 失败怎么查

`connect_test` 发的是**单条 `hello` 消息**（`messages=[{role: user, content: hello}]`），与 random 数据集、
多轮轮数、上下文长度**完全无关**。所以它失败只可能是 URL / api 协议 / key / model 名的问题：

- `Non-retryable error (HTTP 404): HTTP 404 NOT FOUND (empty response body)`：路径不存在，或 model 名
  未在该网关注册。`.../compatible-mode/v1/...` 这类网关常只实现 OpenAI 的 `/v1/chat/completions`，
  Anthropic 的 `/v1/messages` 未必开放 —— 把「API 协议」切到 `openai` 再试；若 OpenAI 协议能通，
  就说明该网关不支持 Anthropic 协议
- `HTTP 403 {"code":16,"msg":"鉴权失败"}`：key 无效 / 过期 / 被限流，与 model 名无关（换任何 model 都 403）
- 日志里 `Elapsed: 01:05` **不是超时**：其中约 63s 是 `random_multi_turn` 生成 30 条 50~100 轮对话的耗时
  （~2.2s/对话），connect_test 在数据集加载完才执行，一执行就快速失败

已做的加固：非 200 响应**只读一次 body**，空 body 时 `error` 会写成
`HTTP 404 NOT FOUND (empty response body)` 而不是空串（以前 `response.json()` 失败后再
`response.text()` 只能拿到空串，日志里会显示成 `(HTTP 404): .` 看不出原因），并对 404 空 body 额外打一条
指向「路径/模型是否存在」的 ERROR。

### 压测里 400 / EOF 的排查口径

网关会把**后端失败统一包成 400**，例如
`400 {"code": 3, "msg": "invalid argument: http post fail: Post \"http://<内网LLM>/v1/chat/completions\": EOF"}`。
这种 400 与请求参数无关，排查顺序是：

1. 看 `msg` 里有没有 `connect: connection refused` / `EOF` / `connection reset` / `502`：
   有就是上游推理服务崩溃、重启或过载，不是你的 body 不合法
2. 看失败是否集中在某个时刻：`history/<run>/.../benchmark_data.db` 里 `start_time` 相邻的
   批量失败 = 服务端崩溃窗口；紧接着新请求全部 `connection refused` 就是重启中
3. 看失败请求的 `turns` 与 `length(request)`：多轮压测里批量失败常常出现在第 40 轮左右、
   body ≈ 200KB（≈45K token）时，是 10 并发长上下文把推理服务打挂

已做的处理：

- `perf_anthropic.py`：失败请求同样写 `completed_time` / `query_latency`，并在 `error` 首行加
  `[failed after X.XXs]`。**秒断（<0.1s）= 连接被拒 / 被网关掐断；数秒后才断 = 等响应头超时
  或上游崩溃**，两者的排查方向完全不同，光看 traceback 区分不出来
- `perf_anthropic.py`：**默认不重试**（`GATEWAY_MAX_RETRIES = 0`）。压测要如实反映全链路稳定性，
  网关把上游失败包成 400/503、连接被拒 / EOF / 超时都直接计入失败请求与响应码分布；
  一旦重试，失败率与 P99 都会被"抹平"成一次成功，结果不可信。
  需要「只看服务端上限、忽略瞬时抖动」时把常量改成 3（1s/2s/4s 指数退避），
  参数类错误（401/403/404/413/字段校验）即使开启也不会重试
- `perf_datasets.py`：用同名子类覆盖 `random_multi_turn`，清洗随机 token 解码出的 `Cc` 控制字符
  （含 `\u0000`，部分网关会当字符串终止符导致 body 截断）与 `Cf` 零宽字符
  （U+200B / U+FEFF / U+00AD / U+202C 等）

另外，`random_multi_turn` 的峰值上下文 ≈ `max_turns × 输入长度`：输入 1000、`min_turns=50/max_turns=100`
时最长一条对话会到 ~11 万 token / 200KB body。前端多轮面板在峰值超过 32,768 tok 时会红字告警，
压测前按告警下调输入长度、轮数或并发。

## 本地 mock 数据集

真实抓取的请求体放在 `datasets/`，由 `mock_dataset.py` 从本地 mock 抓取目录转换而来。
源目录不入库，用 `--src` 指定，或设环境变量 `MOCK_SRC_DIR` 提供默认值：

```
python mock_dataset.py --src <目录>        # 重新生成 datasets/*.jsonl + manifest.json
python mock_dataset.py --src <目录> --out <目录>
```

| 文件 | 行数 | dataset 名 | 用途 |
|---|---|---|---|
| `mock_agent_trace_raw.jsonl` | 368 | `mock_messages` | **保真**完整请求体（`tool_use`/`tool_result` 原样，单请求最大 278K 字符 / 约 73K tokens） |
| `mock_agent_trace_raw_tools300.jsonl` | 49 | `mock_messages` | 只保留工具调用 ≥300 次的快照，单次请求即含 346 次工具往返 |
| `mock_agent_trace.jsonl` | 368 | `mock_messages` | 真实顺序的完整历史快照（工具块已渲染成文本，单请求最大 286K 字符 / 约 71K tokens） |
| `mock_agent_trace_long.jsonl` | 20 | `mock_messages` | 同一批快照按长度降序，`number=1` 就压最长上下文 |
| `mock_agent_multi_turn.jsonl` | 1（22 轮） | `custom_multi_turn` | 22 次真人提问，该轮工具结果内联进 user 文本 |
| `mock_agent_prompt.jsonl` | 1 | `custom` | 整段会话压成的单条 347K 字符 prompt（需把 IO 输入 ≥ 350000 才不会被 `check_prompt_length` 丢弃） |

- `mock_messages` 是本项目新增的数据集插件（`perf_datasets.py`）：**每行一个 messages 数组，整段历史一次性发出**，
  不做轮次切分，用于重放真实 agent 轨迹、测网关与推理服务在固定长上下文下的延迟。
  源会话是 735 条消息（22 次真人提问 + 346 次工具往返）。
- **要压「含几百次工具调用的完整请求」就用 `mock_agent_trace_raw*.jsonl` + `mock_messages`，不要用多轮模式**：
  每行就是「用户第 N 次追问时实际发出的那个 body」，`tool_use` 的 `id` 与 `tool_result` 的 `tool_use_id`
  一一配对（实测 346/346、孤儿 0、单请求 735 消息 / 516KB / 73K token，Anthropic 协议 200）。
- 保真快照**剔除 `thinking` 块**：抓包里的 320 个 thinking 全部没有 `signature`，原样回传真实 Anthropic 会报
  `thinking blocks must include signature`；压测也不需要重放思考内容。剔除后 token 比文本版更小（73K vs 122K）。
- `mock_agent_trace.jsonl` 是**文本形态**：转换时把 `tool_use` / `tool_result` 渲染成
  `[调用工具] name(args)` / `[工具返回] ...`，openai 与 anthropic 两种协议都能发，但会丢失工具协议结构。
- **`custom_multi_turn` 不要用来跑带真实 `tool_use` 的数据集**：它把 `assistant` 当轮次边界后**丢弃内容**
  （`custom.py` 的 `_split_into_turns`），于是 `tool_use` 消失、`role=tool` 变成孤立 `tool_result`，
  第 2 轮就被 Anthropic 判 400 `unexpected tool_use_id found in tool_result blocks`（已实测）。
  它只适合「工具结果已内联成文本」的数据集（如 `mock_agent_multi_turn.jsonl`）。
- `custom_multi_turn` 的 `assistant` 只作轮次边界，多轮压测会让模型重新作答（上下文由真实响应累积）。
- 前端「内置数据集」下拉读取 `GET /datasets`（即 `datasets/manifest.json`），选中后自动填 `dataset` 名称与路径、
  勾选 `dataset` 任务，多轮数据集还会自动打开多轮开关并把 `max_turns` 设为 `min(轮数, 10)`。
- **输入 / 输出 token 组留空或填 0 = 不限制**：选中内置数据集时两列自动清空，
  后台对 dataset 任务不传 `min_prompt_length` / `max_prompt_length`（用 evalscope 默认 0 ~ 131072），
  输出为 0 时 `max_tokens` 传 `None`，请求体里干脆不带该字段（Anthropic 协议除外，它强制要求 `max_tokens`，回退 1024）。
  IO 组整行删光也能提交，后台按 `(0, 0)` 兜底；只有 `random` 任务必须显式给输入长度。

### 高级参数 / 命令导入

- 前端「evalscope 高级参数」面板由 `GET /evalscope/schema` 反射 `evalscope.perf.arguments.Arguments` 动态生成，
  因此 evalscope 升级新增参数时界面自动跟上；留空 = 使用 evalscope 默认值。
  被批量网格接管的字段（`model` / `url` / `api_key` / `parallel` / `number` / `max_tokens` / `dataset*` /
  `outputs_dir` / `tokenizer_path` / `stream` / `prefix_length` / `log_every_n_query` 等）不重复出现。
- 顶部「命令导入」可直接粘贴现成命令：
  ```
  evalscope perf --model qwen3-8b --url http://127.0.0.1:8000/v1/chat/completions \
    --dataset random --tokenizer-path /models/qwen3 --parallel 16 32 --number 80 160 \
    --max-tokens 500 --max-prompt-length 32768 --temperature 0.6
  ```
  解析规则：`--model/--url/--api/--api-key/--tokenizer-path/--dataset/--dataset-path/--max-tokens/
  --min-prompt-length/--max-prompt-length/--parallel/--number/--stream/--multi-turn/
  --min-turns/--max-turns/--multi-turn-args` 映射到批量表单（`--api` → 协议下拉，多轮参数 → 多轮面板），
  其余显式给出的 flag 落入 `evalscope_args`；`--parallel/--number` 多值按位置一一对应展开为多行并发组。
  `--dataset random_multi_turn` 或带 `--multi-turn` 的命令会自动打开多轮开关。
  「由当前表单生成命令」可反向输出等价命令，便于复制到终端。

## 请求详情（benchmark_data.db）

evalscope perf 会把每次请求的请求体与响应写入 `benchmark_data.db`（SQLite 表 `result`）。
页面底部「请求详情」卡片直接读取这份库，无需额外导出：

- **运行 / 子测试**：下拉列出 `history/**/outputs/**/benchmark_data.db`，跑完自动刷新并选中最新一次
- **响应码筛选**：下拉按实际数据动态生成（`全部(n)` / `仅失败` / `4xx` / `5xx` / `200(n)` / `400(n)` / `未知响应码`），
  配合关键字搜索（请求体 / 错误信息 / 响应文本）与分页；失败行淡红高亮，悬停显示错误详情
- **单条详情**：点击任意行打开弹层，分四个标签页——`请求体`（原文 JSON）、`响应内容`
  （思考内容 + 正文）、`原始事件`（流式 SSE 事件数组）、`tool_calls`（合并后的函数调用）

实现要点：

- `request_store.py` 只读打开 SQLite（`file:...?mode=ro`），并校验路径必须位于 `history/` 内
- `response_messages` 是 `base64(pickle(list))`，用受限 `Unpickler`（拒绝任意类）解码，
  同时兼容 OpenAI（`choices[].delta/message`）与 Anthropic（`content_block_*`）两种事件流
- evalscope 原表没有响应码，`runner._patch_evalscope_db()` 会在运行时给建表 / 写库函数打补丁，
  自动 `ALTER TABLE` 补出 `status_code` / `error` / `trace_id` / `turns` / `generated_text` 五列；
  老库缺列时读取侧回退为「成功=200，失败=未知」
- 每个 sub-test 使用独立输出目录（`outputs/<task>_in..._p..._n..._r..._k.../`），
  避免多次子测试覆盖同一个 `benchmark_data.db`

## 输出

每次运行落盘到：

```
history/<YYYYMMDD_HHMMSS>/
├── config.json              # 本次配置快照（api_key / wandb key 脱敏为 ***）
├── results_<model>_<ts>.csv # 23 列 CSV（含 api_key_index / Avg_Turns）
└── outputs/<sub_key>/       # 每个 sub-test 独立目录
    ├── benchmark_summary.json
    └── <run_id>/<model>/<task>/parallel_x_number_y/benchmark_data.db
```

CSV 表头（尾部追加 `Avg_Turns`，单轮为空）：

```
test_timestamp, model_name, api_key_index, task_type, repeat_num, Prompt_length, Max_tokens,
Number_of_concurrency, Total_requests, Succeed_requests, Avg_Latency(s),
Latency_P90(s), TTFT_Avg(s), TTFT_P90(s), TPOT_Avg(s), Avg_Inter_Token_Latency(s),
Avg_Input_Tokens, Avg_Output_Tokens, Avg_Turns, Output_through(tok/s), RPS(req/s),
Total_token_through(tok/s), output_dir
```

> **单位注**：SDK 返回的 percentile 中 ttft/tpot/itl 单位是毫秒，而 BenchmarkSummary 的 `avg_*` 是秒。
> CSV 输出统一为秒，runner 内部已做单位转换。

## 安全收益 vs 旧 shell 架构

| 风险 | 新架构 |
|---|---|
| shell 参数注入 | 走 Pydantic 对象，无字符串拼接 |
| API key 经 env 泄露 | 仅传给 SDK HTTP 客户端 |
| 临时 .sh 落盘含密钥 | 不再生成任何脚本 |
| 系统命令依赖（bash / jq / bc） | 纯 Python |
| WSL vs Git Bash 冲突 | 不再需要 bash |

## random 任务注意事项

选择 `random` 任务时必须填写 `tokenizer_path`。evalscope 的 random dataset 需要 tokenizer 来生成/统计随机 prompt token 长度；未填写时前端会在提交前拦截，后端也会在 `/start` 阶段拒绝启动并返回明确错误信息。

## 限制（demo 阶段）

- **单任务**：同一时刻只允许一个 BenchmarkRunner 实例（受 `_thread.is_alive()` 保护）
- **停止粒度**：当前 `run_perf_benchmark` 调用同步阻塞，要等当前 sub-test 跑完才能退到下一次 stop 检查点
- **本地监听**：仅 127.0.0.1，无鉴权
- **历史 CSV**：仅落盘，不在前端可视化（单次请求明细可在「请求详情」里查看）
