"""Vastar runtime component."""

from __future__ import annotations

from core.llm.base import LLMRequest
from core.llm.providers.openai_compat import OpenAICompatProvider


class GLMProvider(OpenAICompatProvider):
    name = "glm"

    def amend_body(self, body: dict, request: LLMRequest) -> dict:
        thinking = request.params.get("thinking")
        if thinking:
            body["thinking"] = thinking
        return body
