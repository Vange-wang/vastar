"""DeepSeek 适配器（主 provider，OpenAI 兼容直连）。

A1（官方文档，采纳件 §二）：思考模式不支持 temperature/presence_penalty/frequency_penalty
——设置不报错但无效果。实现照发不报错（任务单 §⑤），但不得据此宣称已控制随机性。
"""

from __future__ import annotations

from core.llm.providers.openai_compat import OpenAICompatProvider


class DeepSeekProvider(OpenAICompatProvider):
    name = "deepseek"
