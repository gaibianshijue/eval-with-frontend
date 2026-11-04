"""Anthropic Messages API 压测适配插件（注册名 ``anthropic``）。

evalscope perf 原生只提供 openai 系插件（``openai`` / ``openai_responses`` /
``openai_embedding`` / ``openai_rerank`` / ``dashscope`` / ``local*``），
请求体与响应解析都写死为 OpenAI 形状（``choices`` / ``usage.prompt_tokens``）。
本模块补一个最小可用的 Anthropic 适配层：

* 请求：``POST {url}/v1/messages``
  - ``system`` 消息抽离为顶层 ``system`` 字段
  - ``messages`` 归一化为 ``[{role, content}]``，连续同角色自动合并
  - ``role=tool`` → ``tool_result`` block，``assistant.tool_calls`` → ``tool_use`` block
  - ``max_tokens`` 必填（Anthropic 强制要求），缺省回退 1024
  - OpenAI 专属字段（``ignore_eos`` / ``stream_options`` 等）自动剔除
* 鉴权：``x-api-key`` + ``anthropic-version``（覆盖 evalscope 自动注入的 ``Authorization``）
* 响应：解析 ``message_start`` / ``content_block_delta`` / ``message_delta`` 事件，
  按 ``usage.input_tokens`` / ``usage.output_tokens`` 统计 token，TTFT 取首个内容增量

用法::

    import perf_anthropic  # noqa: F401  触发 @register_api('anthropic')
    # 然后 Arguments(api='anthropic', url='https://api.anthropic.com/v1/messages', ...)
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple, Union

from evalscope.perf.arguments import Arguments
from evalscope.perf.plugin.api.default_api import DefaultApiPlugin, StreamedResponseHandler
from evalscope.perf.plugin.registry import register_api
from evalscope.perf.utils.benchmark_util import BenchmarkData
from evalscope.utils.logger import get_logger

logger = get_logger()

ANTHROPIC_VERSION = '2023-06-01'
DEFAULT_MAX_TOKENS = 1024

#: OpenAI 专属参数，Anthropic 不识别，直接丢弃避免 400（Anthropic 默认拒绝未知字段）
DROPPED_EXTRA_KEYS = {
    'ignore_eos', 'ignore_length', 'stream_options', 'min_tokens',
    'repetition_penalty', 'tasks', 'n', 'seed', 'logprobs',
    'top_logprobs', 'stop_token_ids', 'chat_template_kwargs',
}

#: 网关/上游瞬时故障的重试次数与退避基数。
#:
#: **默认 0 = 不重试**：压测要如实反映全链路稳定性，网关把上游失败包成 400/503、
#: 或连接被拒/EOF/超时，都应当作为一次失败请求计入 `成功/失败` 与响应码分布。
#: 一旦重试，失败会被"抹平"成一次成功，失败率与 P99 都不可信。
#: 需要「只看服务端上限、忽略瞬时抖动」时把这里调成 3（指数退避
#: ``GATEWAY_RETRY_BASE_DELAY * 2**attempt``），参数类 4xx（401/403/404/413/字段校验）即使开启也不会重试。
GATEWAY_MAX_RETRIES = 0
GATEWAY_RETRY_BASE_DELAY = 1.0

#: 网关把上游失败包装成 HTTP 400 时的特征串（如 ``{"code": 3, "msg": "invalid argument: http post fail: ... EOF"}``）
_GATEWAY_RETRYABLE_RE = re.compile(
    r'http post fail|connection refused|connect: |EOF|connection reset|broken pipe|'
    r'server disconnected|timed out|timeout|no such host|502|503|504|'
    r'bad gateway|service unavailable|temporarily unavailable',
    re.IGNORECASE,
)


def is_retryable_gateway_error(status_code: Optional[int], error: Optional[str]) -> bool:
    """判断失败是否属于「上游网关/网络瞬时故障」，而不是请求体本身不合法。"""
    if status_code in (429, 500, 502, 503, 504):
        return True
    if status_code is None:  # 没拿到状态码：连接失败 / 读超时 / 服务端直接断流
        return True
    if status_code == 400:  # 网关常把后端失败统一包成 400，只能靠 error 文本区分
        return bool(_GATEWAY_RETRYABLE_RE.search(error or ''))
    return False


def _as_int(v) -> int:
    """max_tokens 可能是 int 或区间列表（evalscope 的 --max-tokens 支持多值）。"""
    if isinstance(v, (list, tuple)):
        return int(random.choice(list(v))) if v else DEFAULT_MAX_TOKENS
    return int(v)


def _image_block(image_url: Any) -> Dict[str, Any]:
    """OpenAI ``image_url`` → Anthropic ``image`` block（仅支持 base64 data url）。"""
    url = image_url.get('url') if isinstance(image_url, dict) else image_url
    if isinstance(url, str) and url.startswith('data:') and ';base64,' in url:
        media_type, b64 = url[5:].split(';base64,', 1)
        return {
            'type': 'image',
            'source': {'type': 'base64', 'media_type': media_type, 'data': b64},
        }
    return {'type': 'text', 'text': f'[image: {url}]'}


def _to_blocks(content: Any) -> List[Dict[str, Any]]:
    """把 string / OpenAI content parts 归一化为 Anthropic content blocks。"""
    if content is None:
        return []
    if isinstance(content, str):
        return [{'type': 'text', 'text': content}] if content else []
    if isinstance(content, list):
        blocks: List[Dict[str, Any]] = []
        for part in content:
            if isinstance(part, str):
                if part:
                    blocks.append({'type': 'text', 'text': part})
            elif isinstance(part, dict):
                ptype = part.get('type')
                if ptype in ('text', 'image', 'tool_use', 'tool_result', 'document', 'thinking'):
                    blocks.append(part)
                elif ptype == 'image_url' or 'image_url' in part:
                    blocks.append(_image_block(part.get('image_url')))
                elif 'text' in part:
                    blocks.append({'type': 'text', 'text': str(part['text'])})
                else:
                    blocks.append({'type': 'text', 'text': json.dumps(part, ensure_ascii=False)})
        return blocks
    return [{'type': 'text', 'text': str(content)}]


def _assistant_blocks(message: Dict[str, Any]) -> List[Dict[str, Any]]:
    """assistant 消息：文本内容 + OpenAI ``tool_calls`` → Anthropic ``tool_use``。"""
    blocks = _to_blocks(message.get('content'))
    for idx, call in enumerate(message.get('tool_calls') or []):
        if not isinstance(call, dict):
            continue
        fn = call.get('function') or {}
        args = fn.get('arguments')
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except json.JSONDecodeError:
                args = {'raw': args}
        blocks.append({
            'type': 'tool_use',
            'id': call.get('id') or f'toolu_{idx}',
            'name': fn.get('name') or 'tool',
            'input': args if isinstance(args, dict) else {'value': args},
        })
    return blocks


def normalize_conversation(messages: Any) -> Tuple[str, List[Dict[str, Any]]]:
    """OpenAI 风格 messages → (system_text, anthropic messages)。"""
    if isinstance(messages, str):
        return '', [{'role': 'user', 'content': _to_blocks(messages)}]
    if isinstance(messages, dict):
        messages = [messages]

    system_parts: List[str] = []
    conversation: List[Dict[str, Any]] = []

    def _push(role: str, blocks: List[Dict[str, Any]]) -> None:
        if not blocks:
            return
        if conversation and conversation[-1]['role'] == role:
            conversation[-1]['content'].extend(blocks)
        else:
            conversation.append({'role': role, 'content': blocks})

    for message in messages or []:
        if not isinstance(message, dict):
            _push('user', _to_blocks(str(message)))
            continue
        role = str(message.get('role') or 'user').lower()
        content = message.get('content')

        if role == 'system':
            text = content if isinstance(content, str) else '\n'.join(
                b.get('text', '') for b in _to_blocks(content)
            )
            if text:
                system_parts.append(text)
            continue

        if role == 'tool':
            block = {
                'type': 'tool_result',
                'tool_use_id': message.get('tool_call_id') or 'toolu_0',
            }
            if isinstance(content, str):
                block['content'] = content
            else:
                block['content'] = _to_blocks(content)
            _push('user', [block])
            continue

        if role == 'assistant':
            _push('assistant', _assistant_blocks(message))
        else:
            _push('user', _to_blocks(content))

    if not conversation:
        conversation = [{'role': 'user', 'content': [{'type': 'text', 'text': ''}]}]
    # Anthropic 要求首条尽量是 user；assistant 开头（prefill）也允许，保持原样
    return '\n\n'.join(system_parts), conversation


def convert_tools(tools: Any) -> List[Dict[str, Any]]:
    """OpenAI ``tools`` → Anthropic ``tools``（``parameters`` → ``input_schema``）。

    已是 Anthropic 形状（带 ``input_schema``）的条目原样保留，便于混用。
    """
    if not isinstance(tools, list):
        return []
    converted: List[Dict[str, Any]] = []
    for item in tools:
        if not isinstance(item, dict):
            continue
        if 'input_schema' in item:  # 已是 Anthropic 形状
            converted.append(item)
            continue
        fn = item.get('function') if isinstance(item.get('function'), dict) else item
        name = fn.get('name')
        if not name:
            continue
        tool: Dict[str, Any] = {
            'name': name,
            'input_schema': fn.get('parameters') or {'type': 'object', 'properties': {}},
        }
        if fn.get('description'):
            tool['description'] = fn['description']
        converted.append(tool)
    return converted


def convert_tool_choice(tool_choice: Any) -> Optional[Dict[str, Any]]:
    """OpenAI ``tool_choice`` → Anthropic ``tool_choice``；无法映射时返回 None。"""
    if isinstance(tool_choice, dict):
        if 'type' in tool_choice:
            return tool_choice
        fn = tool_choice.get('function') or tool_choice
        if isinstance(fn, dict) and fn.get('name'):
            return {'type': 'tool', 'name': fn['name']}
        return None
    if not isinstance(tool_choice, str):
        return None
    value = tool_choice.strip()
    if not value:
        return None
    lowered = value.lower()
    if lowered in ('auto', 'any', 'required'):
        return {'type': 'auto' if lowered == 'auto' else 'any'}
    if lowered in ('none', 'off'):
        return None
    return {'type': 'tool', 'name': value}


def normalize_url(url: str) -> str:
    """把用户填的 URL 归一化成 ``.../v1/messages``。"""
    s = (url or '').strip().rstrip('/')
    if not s:
        return s
    if s.endswith('/messages'):
        return s
    if s.endswith('/chat/completions'):
        return s[: -len('/chat/completions')] + '/messages'
    if s.endswith('/v1'):
        return s + '/messages'
    if '/v1/' in s:
        return s + '/messages'
    return s + '/v1/messages'


def _extract_data(message: str) -> Optional[Dict[str, Any]]:
    """从一条 SSE 分帧里取出 ``data:`` 载荷（Anthropic 会带 ``event:`` 行）。"""
    if message.startswith(':'):
        return None
    payload_lines = [
        line[len('data:'):].strip()
        for line in message.splitlines()
        if line.startswith('data:')
    ]
    if not payload_lines:
        # 极少数网关直接返回裸 JSON
        text = message.strip()
        if not text.startswith('{'):
            return None
        payload_lines = [text]
    raw = '\n'.join(payload_lines).strip()
    if not raw or raw == '[DONE]':
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


#: 响应头只留前 N 个键、单值截断，避免网关塞超长 header 撑爆 db。
_HEADER_LIMIT = 40
_HEADER_VALUE_LIMIT = 512


def _headers_dict(response) -> Dict[str, str]:
    """把 aiohttp 的响应头转成普通 dict（供前端「响应头」标签展示）。

    用来定位链路问题：网关的 ``x-request-id`` / ``x-ratelimit-*`` / 上游耗时头
    只能从这里看到，响应体里没有。重名 header 以后者为准（与 ``dict()`` 一致）。
    """
    try:
        items = list(response.headers.items())
    except Exception:  # noqa: BLE001
        return {}
    return {
        str(k): str(v)[:_HEADER_VALUE_LIMIT]
        for k, v in items[:_HEADER_LIMIT]
    }


def _apply_usage(output: BenchmarkData, usage: Optional[Dict[str, Any]]) -> None:
    if not usage:
        return
    if usage.get('input_tokens') is not None:
        output.prompt_tokens = int(usage['input_tokens'])
    if usage.get('output_tokens') is not None:
        output.completion_tokens = int(usage['output_tokens'])
    cached = usage.get('cache_read_input_tokens')
    if cached is not None:
        output.real_cached_tokens = int(cached)


@register_api('anthropic')
class AnthropicPlugin(DefaultApiPlugin):
    """Anthropic ``/v1/messages`` 协议插件。"""

    def __init__(self, param: Arguments):
        super().__init__(param)
        self.tokenizer = None
        if param.tokenizer_path:
            try:
                from evalscope.perf.plugin.datasets.utils import load_tokenizer
                self.tokenizer = load_tokenizer(param.tokenizer_path)
            except Exception as e:  # noqa: BLE001
                logger.warning(f'[anthropic] tokenizer 加载失败，token 统计回退为字符估算: {e}')

        # --- 鉴权头：x-api-key 取代 Authorization ---
        headers = {
            k: v for k, v in (param.headers or {}).items()
            if k.lower() not in ('authorization', 'x-api-key')
        }
        if param.api_key:
            headers['x-api-key'] = param.api_key
        headers.setdefault('anthropic-version', ANTHROPIC_VERSION)
        param.headers = headers

        # --- URL 归一化 ---
        new_url = normalize_url(param.url)
        if new_url != param.url:
            logger.info(f'[anthropic] URL 自动补全为 {new_url}')
            param.url = new_url
        # anthropic 端点按 message dict 发送，必须走 chat 模板路径
        param.apply_chat_template = True

    # ------------------------------------------------------------------ 请求
    def build_request(
        self, messages: Union[List[Dict], str, List[int], Dict], param: Optional[Arguments] = None
    ) -> Dict:
        param = param or self.param
        try:
            if isinstance(messages, list) and messages and isinstance(messages[0], int):
                logger.warning('[anthropic] 不支持 token-ID 输入（--tokenize-prompt），已按空消息发送')
                messages = [{'role': 'user', 'content': ''}]

            system_text, conversation = normalize_conversation(messages)

            payload: Dict[str, Any] = {}
            if param.query_template:
                raw = param.query_template
                if raw.startswith('@'):
                    with open(raw[1:], encoding='utf-8') as f:
                        payload = json.load(f)
                else:
                    payload = json.loads(raw)
                payload['messages'] = conversation
                if system_text:
                    payload.setdefault('system', system_text)
            else:
                payload['messages'] = conversation
                if system_text:
                    payload['system'] = system_text

            payload['model'] = param.model
            if param.max_tokens is not None:
                payload['max_tokens'] = _as_int(param.max_tokens)
            else:
                payload.setdefault('max_tokens', DEFAULT_MAX_TOKENS)

            if param.temperature is not None:
                payload['temperature'] = param.temperature
            if param.top_p is not None:
                payload['top_p'] = param.top_p
            if param.top_k is not None:
                payload['top_k'] = int(param.top_k)
            if param.stop:
                payload['stop_sequences'] = (
                    list(param.stop) if isinstance(param.stop, (list, tuple)) else [str(param.stop)]
                )
            if param.stream:
                payload['stream'] = True

            extras = dict(param.extra_args or {})
            # tools / tool_choice：OpenAI 形状 → Anthropic 形状（其余 key 原样合并）
            tools = convert_tools(extras.pop('tools', None))
            if tools:
                payload['tools'] = tools
            choice = convert_tool_choice(extras.pop('tool_choice', None))
            if choice:
                payload['tool_choice'] = choice
            for key, value in extras.items():
                if key in DROPPED_EXTRA_KEYS or value is None:
                    continue
                payload[key] = value
            payload.setdefault('max_tokens', DEFAULT_MAX_TOKENS)
            return payload
        except Exception as e:  # noqa: BLE001
            logger.exception(e)
            return None

    # ------------------------------------------------------------------ 响应
    async def process_request(self, client_session, url, headers, body) -> BenchmarkData:
        """单次测量。``GATEWAY_MAX_RETRIES=0`` 时默认不重试，失败原样计入结果。

        压测关心的是"这条链路到底稳不稳"，所以网关 400/503、连接被拒、EOF 等
        都直接返回失败，不做重发。把 ``GATEWAY_MAX_RETRIES`` 调大才会退避重试；
        即使调大，参数类错误（401/403/404/413/字段校验）也永不重试。
        """
        retries = max(0, int(GATEWAY_MAX_RETRIES))
        output: BenchmarkData = BenchmarkData()
        for attempt in range(retries + 1):
            output = await self._post_once(client_session, url, headers, body)
            if output.success:
                return output
            if attempt >= retries or not is_retryable_gateway_error(
                output.status_code, output.error
            ):
                return output
            delay = GATEWAY_RETRY_BASE_DELAY * (2**attempt)
            logger.warning(
                f'上游网关瞬时失败(status={output.status_code})，{delay:.0f}s 后重试 '
                f'{attempt + 1}/{retries}：{(output.error or "")[:200]}'
            )
            await asyncio.sleep(delay)
        return output

    async def _post_once(self, client_session, url, headers, body) -> BenchmarkData:
        headers = {'Content-Type': 'application/json', **headers}
        data = json.dumps(body, ensure_ascii=False) if not isinstance(body, str) else body

        output = BenchmarkData()
        st = time.perf_counter()
        output.start_time = st
        output.request = data
        last = st
        ttft = 0.0
        generated = ''

        try:
            async with client_session.post(url=url, data=data, headers=headers) as response:
                # 非 200 也要记：排障时网关的 x-request-id / 限流头往往只在错误响应上
                output.response_headers = _headers_dict(response)
                content_type = response.headers.get('Content-Type', '')
                if response.status != 200:
                    output.status_code = response.status
                    # body 只能读一次：先取 text，再尝试当 JSON 美化，避免 response.json()
                    # 失败后 response.text() 拿到空串（404 空 body 时 error 会变成 ''）
                    raw = (await response.text() or '').strip()
                    if raw:
                        try:
                            detail = json.dumps(json.loads(raw), ensure_ascii=False)
                        except Exception:  # noqa: BLE001
                            detail = raw
                    else:
                        detail = f'HTTP {response.status} {response.reason or ""} (empty response body)'.strip()
                    output.error = detail[:2000]
                    output.success = False
                    if response.status == 404 and not raw:
                        logger.error(
                            f'[anthropic] {url} 返回 404 且响应体为空：该路径大概率不存在。'
                            '请确认网关是否提供 Anthropic /v1/messages 端点（可先试 OpenAI 兼容'
                            ' /v1/chat/completions），以及 model 名是否在该网关注册。'
                        )
                    return output

                if 'text/event-stream' in content_type:
                    handler = StreamedResponseHandler()
                    async for chunk in response.content.iter_any():
                        if not chunk:
                            continue
                        for message in handler.add_chunk(chunk):
                            payload = _extract_data(message)
                            if payload is None:
                                continue
                            now = time.perf_counter()
                            etype = payload.get('type')

                            if etype == 'message_start':
                                _apply_usage(output, (payload.get('message') or {}).get('usage'))
                                output.response_messages.append(payload)
                                continue

                            if etype == 'content_block_start':
                                block = payload.get('content_block') or {}
                                if block.get('type') == 'tool_use' and ttft == 0.0:
                                    ttft = now - st
                                    output.first_chunk_latency = ttft
                                    last = now
                                output.response_messages.append(payload)
                                continue

                            if etype == 'content_block_delta':
                                delta = payload.get('delta') or {}
                                piece = delta.get('text') or delta.get('thinking') or ''
                                if piece:
                                    if ttft == 0.0:
                                        ttft = now - st
                                        output.first_chunk_latency = ttft
                                    else:
                                        output.inter_chunk_latency.append(now - last)
                                    generated += piece
                                    last = now
                                output.response_messages.append(payload)
                                continue

                            if etype == 'message_delta':
                                _apply_usage(output, payload.get('usage'))
                                output.response_messages.append(payload)
                                continue

                            if etype == 'error':
                                err = payload.get('error') or {}
                                raise RuntimeError(
                                    f"Anthropic stream error: {err.get('type', 'unknown')}: {err.get('message', '')}"
                                )

                    end = last if last > st else time.perf_counter()
                    output.completed_time = end
                    output.query_latency = max(end - st, 0.0)
                    if not output.first_chunk_latency:
                        # 无文本增量（如纯 tool_use 或被截断），退化用总耗时
                        output.first_chunk_latency = output.query_latency
                    output.generated_text = generated
                    output.success = True
                    return output

                # ---- 非流式 ----
                payload: Any
                try:
                    payload = await response.json()
                except Exception:  # noqa: BLE001
                    payload = await response.text()

                now = time.perf_counter()
                output.completed_time = now
                output.query_latency = now - st
                output.first_chunk_latency = output.query_latency

                if isinstance(payload, dict):
                    if payload.get('type') == 'error':
                        output.success = False
                        output.error = json.dumps(payload, ensure_ascii=False)
                        output.response_messages.append(payload)
                        return output
                    blocks = payload.get('content') or []
                    output.generated_text = ''.join(
                        b.get('text', '') for b in blocks if isinstance(b, dict) and b.get('type') == 'text'
                    )
                    _apply_usage(output, payload.get('usage'))
                    output.response_messages.append(payload)
                else:
                    output.generated_text = str(payload)
                output.success = True
                return output
        except Exception:  # noqa: BLE001
            output.success = False
            output.error = ''.join(traceback.format_exception(*__import__('sys').exc_info()))
            logger.error(output.error)
        return output

    def parse_responses(
        self, responses: List[Dict], request: str = None, **kwargs: Any
    ) -> Tuple[int, int]:
        """从 Anthropic 响应中取 usage；没有 usage 时用 tokenizer 兜底。"""
        input_tokens: Optional[int] = None
        output_tokens: Optional[int] = None
        texts: List[str] = []

        for item in responses or []:
            if not isinstance(item, dict):
                continue
            usage = item.get('usage') or (item.get('message') or {}).get('usage')
            if isinstance(usage, dict):
                if usage.get('input_tokens') is not None:
                    input_tokens = int(usage['input_tokens'])
                if usage.get('output_tokens') is not None:
                    output_tokens = int(usage['output_tokens'])
            if item.get('type') == 'content_block_delta':
                texts.append((item.get('delta') or {}).get('text') or '')
            for block in item.get('content') or []:
                if isinstance(block, dict) and block.get('type') == 'text':
                    texts.append(block.get('text') or '')

        if input_tokens is not None and output_tokens is not None:
            return input_tokens, output_tokens

        text = ''.join(texts)
        if self.tokenizer is not None:
            if input_tokens is None:
                try:
                    req = json.loads(request or '{}')
                    prompt = json.dumps(req.get('messages') or req.get('system') or '', ensure_ascii=False)
                    input_tokens = len(self.tokenizer.encode(prompt, add_special_tokens=False))
                except Exception:  # noqa: BLE001
                    input_tokens = 0
            if output_tokens is None:
                try:
                    output_tokens = len(self.tokenizer.encode(text, add_special_tokens=False))
                except Exception:  # noqa: BLE001
                    output_tokens = 0
            return int(input_tokens or 0), int(output_tokens or 0)

        # 无 tokenizer：按 4 字符 ≈ 1 token 粗估，避免指标全为 0
        approx_in = len(request or '') // 4 if input_tokens is None else input_tokens
        approx_out = len(text) // 4 if output_tokens is None else output_tokens
        return int(approx_in or 0), int(approx_out or 0)


def register() -> None:
    """显式注册（import 时已自动注册，保留幂等入口方便调用）。"""
    from evalscope.perf.plugin.registry import ApiRegistry
    if 'anthropic' not in ApiRegistry.all_classes():
        ApiRegistry.register('anthropic', AnthropicPlugin)
