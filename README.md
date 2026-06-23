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
| `GET  /stream` | SSE 流式日志 |

## 配置项 → SDK Arguments 映射

| 前端字段 | `evalscope.perf.Arguments` |
|---|---|
| model_name | `model` |
| api_url | `url` |
| tokenizer_path | `tokenizer_path`（选择 `random` 任务时必填，等价于 evalscope `--tokenizer-path`） |
| openai_api_key | `api_key`（仅传给 SDK，不落盘） |
| openclaw_dataset_path | `dataset_path` |
| openclaw_dataset_name | `dataset`（默认 `line_by_line`） |
| input_output_groups[].input | `min_prompt_length` / `max_prompt_length`（random 任务） |
| input_output_groups[].output | `max_tokens` |
| concurrency_request_groups[].parallel | `parallel` |
| concurrency_request_groups[].number | `number` |
| log_every_n_query | `log_every_n_query` |
| prefix_length | `prefix_length` |
| wait_between_tests | 各 sub-test 之间 sleep（runner 自管） |
| task_types | random / openclaw（runner 内分支处理；`random` 需要同时提供 `tokenizer_path`） |

固定参数：`api="openai"`，`extra_args={"ignore_eos": True}`，`read_timeout=600`，`connect_timeout=30`，`enable_progress_tracker=True`。

## 输出

每次运行落盘到：

```
web_demo/history/<YYYYMMDD_HHMMSS>/
├── config.json              # 本次配置快照（api_key 脱敏为 ***）
├── results_<model>_<ts>.csv # 与原 eval_bench0522.sh 格式一致的 21 列 CSV
└── outputs/                 # evalscope 自己的产物（benchmark_summary.json 等）
```

CSV 表头（与原脚本完全一致，便于已有下游工具继续用）：

```
test_timestamp, model_name, task_type, repeat_num, Prompt_length, Max_tokens,
Number_of_concurrency, Total_requests, Succeed_requests, Avg_Latency(s),
Latency_P90(s), TTFT_Avg(s), TTFT_P90(s), TPOT_Avg(s), Avg_Inter_Token_Latency(s),
Avg_Input_Tokens, Avg_Output_Tokens, Output_through(tok/s), RPS(req/s),
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
- **历史 CSV**：仅落盘，不在前端可视化
