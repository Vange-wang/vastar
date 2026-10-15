"""Vastar runtime component."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from core.llm.capabilities import CAPABILITIES, CapabilityClaim, configured_contract


class FrameKind:
    REASONING = "reasoning"
    CONTENT = "content"
    USAGE = "usage"
    FINISH = "finish"
    HEARTBEAT = "heartbeat"


@dataclass
class LLMRequest:
    provider: str
    model: str
    messages: list[dict]           # OpenAI 格式（阶段一不回传 reasoning_content）
    params: dict                   # temperature/top_p/max_tokens + provider 专属参数
    timeouts: dict                 # connect/first_delta/stream_idle/attempt_total/run_total/retry_wait_budget
    meta: dict = field(default_factory=dict)   # run_id / attempt_id / phase 等（不发送给供应商）
    tools: list | None = None      # function-calling schema（启用工具时注入；R19 标准形态）


@dataclass
class ProviderFrame:
    kind: str
    text: str = ""
    source_chunk_index: int = 0
    finish_reason: str | None = None
    usage: dict | None = None
    tool_calls: list | None = None


@dataclass
class ProviderSpec:
    """一个已装配可用的 provider：对象 + 该供应商自己的参数与预算（不跨供应商共用）。"""

    name: str                       # deepseek / glm
    provider: LLMProvider
    model: str
    params: dict                    # temperature/top_p/max_tokens + provider 专属
    timeouts: dict
    profile: object                 # BudgetProfile（token 预算按 provider 推导）
    capabilities: dict = field(default_factory=dict)
    pricing: dict = field(default_factory=dict)  # per-million input/output + currency/version；缺失 unknown

    def __post_init__(self):
        if not self.capabilities:
            self.capabilities = configured_contract(self.model, self.profile, self.provider)
        if set(self.capabilities) != set(CAPABILITIES):
            raise ValueError("ProviderSpec 必须声明完整六项能力")
        if not all(isinstance(c, CapabilityClaim) for c in self.capabilities.values()):
            raise ValueError("能力必须使用带验证出处的 CapabilityClaim")


class LLMProvider(ABC):
    """统一供应商接口：stream(request) -> AsyncIterator[ProviderFrame]。

    实现方保证：不自行重试、不自行切备（那是 RetryPolicy 的职责）；
    收到取消时尽快停止读取并关闭连接；错误以 VastarError 抛出（带分类）。
    """

    name: str = "base"
    # B07 协议能力声明：是否接受"无 tools 参数但 messages 含历史 tool 消息"
    #（DeepSeek/GLM openai 兼容均接受）。False → 续写构建请求时**显式协议适配**
    #（tool 观察转换为带标记的文本证据 + warnings 留痕），禁止默默丢掉。
    supports_tool_messages_without_tools: bool = True

    @abstractmethod
    def stream(self, request: LLMRequest) -> AsyncIterator[ProviderFrame]:
        """流式发起一次请求；结束前必产出 finish 帧（合法结束时）。"""
        raise NotImplementedError
