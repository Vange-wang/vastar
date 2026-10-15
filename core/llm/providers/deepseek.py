"""Vastar runtime component."""

from __future__ import annotations

from core.llm.providers.openai_compat import OpenAICompatProvider


class DeepSeekProvider(OpenAICompatProvider):
    name = "deepseek"
