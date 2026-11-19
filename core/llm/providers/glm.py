"""智谱 GLM 适配器（备 provider，OpenAI 兼容）。

裁定口径（2026-09-20-model-baseline_hm.md）：
- Model Code = glm-5.3-flash；base_url = https://open.bigmodel.cn/api/paas/v4/；
- 思考模式始终开启（thinking.type 仅支持 enabled），显式随请求发送；
- temperature 区间 (0,1]，与 DeepSeek 不通用，参数只取自 provider 自己的配置块。
"""

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
