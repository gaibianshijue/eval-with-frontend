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
| `app.py` | Flask 路由层：`/start`、`/stop`、`/status`、`/state`、`/stream`(SSE)、`/results`、`/download/csv`、`/evalscope/schema`、`/evalscope/parse-command` |
| `runner.py` | 核心业务：`RunConfig` 配置校验、`BenchmarkRunner` 单例管理压测生命周期、结果广播；`evalscope_args` 透传其余 `Arguments` 字段 |
| `evalscope_schema.py` | 反射 `Arguments` 生成参数元信息（供前端动态渲染高级面板）；解析 `evalscope perf ...` 命令回填表单 |
| `csv_writer.py` | CSV 输出：21 列表头，`write_run`/`write_avg`，注意 `avg_ttft`/`avg_tpot`/`avg_itl` 单位是 ms，CSV 统一转 s |
| `templates/index.html` | 单页前端骨架（命令导入 / 连接 / 数据集 / 批量网格 / 执行参数 / 高级参数 + 右侧进度日志 + 结果表） |
| `static/app.css` `static/core.js` `static/run.js` `static/advanced.js` | 样式、表单核心、运行态（日志/结果/SSE/进度）、高级参数面板与命令解析 |

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
| `GET /status` | JSON | 进度（current/total/task/IO/并发/ETA） |
| `GET /state` | JSON | running/state/run_dir/csv_path |
| `GET /stream` | SSE | 实时日志流 |
| `GET /results` | JSON | 累积结果行列表 |
| `GET /download/csv` | File | 下载 CSV |

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
