"""evalscope perf 数据集插件：把「完整 messages 快照」作为单次请求重放。

``mock_messages``
-----------------
每行是一个 JSON 数组（OpenAI 形态的 messages），整行会作为**一次请求**的
历史原样发出，用于重放真实 agent 轨迹（例如 735 条消息、28 万字符的上下文），
测的是网关 / 推理服务在固定长上下文下的延迟，而不是让模型重新生成历史。

与 ``custom_multi_turn`` 的区别：
* ``custom_multi_turn``：每行是一条对话，按轮切分后由 runner 逐轮累加**模型真实回复**
* ``mock_messages``：每行已经是完整历史，一次请求发完，不做轮次切分

用法::

    evalscope perf --dataset mock_messages --dataset-path datasets/mock_agent_trace.jsonl \\
        --url ... --parallel 1 --number 5 --max-tokens 500
"""

from __future__ import annotations

import json
import unicodedata
from typing import Any, Dict, Iterator, List

from evalscope.perf.arguments import Arguments
from evalscope.perf.plugin.datasets.base import DatasetPluginBase, Messages
from evalscope.perf.plugin.datasets.multi_turn import RandomMultiTurnDatasetPlugin
from evalscope.perf.plugin.registry import register_dataset
from evalscope.utils import get_logger

logger = get_logger()


@register_dataset('mock_messages')
class MockMessagesDatasetPlugin(DatasetPluginBase):
    """读取「每行一个 messages 数组」的 JSONL，整段历史作为一次请求。"""

    def __init__(self, query_parameters: Arguments):
        super().__init__(query_parameters)
        if not query_parameters.dataset_path:
            raise ValueError('mock_messages 需要 --dataset-path 指向 JSONL（每行一个 messages 数组）')

    def _validate(self, messages: Any) -> bool:
        if not isinstance(messages, list) or not messages:
            return False
        for m in messages:
            if not isinstance(m, dict) or 'role' not in m or 'content' not in m:
                return False
        return True

    def build_messages(self) -> Iterator[Messages]:
        path = self.query_parameters.dataset_path
        for lineno, line in enumerate(self.dataset_line_by_line(path), 1):
            line = line.strip()
            if not line:
                continue
            try:
                messages: List[Dict[str, Any]] = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning(f'mock_messages: 跳过第 {lineno} 行（JSON 解析失败: {exc}）')
                continue
            if not self._validate(messages):
                logger.warning(f'mock_messages: 跳过第 {lineno} 行（需要非空的 messages 数组，每项含 role/content）')
                continue
            yield messages


# ---------------------------------------------------------------- random_multi_turn
# evalscope 的 random_multi_turn 用随机 token id 解码出 prompt，解码结果里会混入：
#   * Cc 控制字符：0x00–0x1f、0x7f（其中 \u0000 会被部分网关当字符串终止符 → body 截断 → 400/EOF）
#   * Cf 零宽/方向控制字符：U+200B、U+FEFF(BOM)、U+00AD、U+202C、U+2062
# 这两类字符对压测毫无意义，却可能让上游解析失败。这里在数据集层统一清洗，
# 只删不可见字符，可见文本与 token 数量基本不变。
_KEEP_CONTROL = {'\n', '\r', '\t'}


def sanitize_prompt_text(text: str) -> str:
    """剔除随机 prompt 里的控制字符与零宽格式字符，返回可安全序列化的文本。"""
    if not isinstance(text, str):
        return text
    kept = []
    for ch in text:
        category = unicodedata.category(ch)
        if category == 'Cc' and ch not in _KEEP_CONTROL:
            continue
        if category == 'Cf':  # 零宽空格/BOM/软连字符/方向控制等，压测里纯噪声
            continue
        if category == 'Cs':  # 孤立代理项：JSON 里非法，直接丢弃
            continue
        kept.append(ch)
    return ''.join(kept)


@register_dataset('random_multi_turn')
class SanitizedRandomMultiTurnDatasetPlugin(RandomMultiTurnDatasetPlugin):
    """官方 ``random_multi_turn`` + 不可见字符清洗（同名覆盖注册，其余行为完全继承）。"""

    def build_messages(self) -> Iterator[List[Any]]:  # type: ignore[override]
        for conversation in super().build_messages():
            for turn in conversation:
                for message in turn.messages:
                    content = message.get('content')
                    if isinstance(content, str):
                        message['content'] = sanitize_prompt_text(content)
            yield conversation
