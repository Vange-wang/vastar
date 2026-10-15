"""Vastar runtime component."""

from __future__ import annotations

from dataclasses import dataclass, field


class ErrorCode:
    CONTEXT_BUDGET_EXCEEDED = "CONTEXT_BUDGET_EXCEEDED"
    INPUT_BUDGET_EXCEEDED = "INPUT_BUDGET_EXCEEDED"   # B06：工具请求超输入预算（显式拦截、请求不发出）
    TOKEN_GUARANTEE_UNAVAILABLE = "TOKEN_GUARANTEE_UNAVAILABLE"
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    TOOL_REJECTED = "TOOL_REJECTED"              
    TOOL_EXECUTION_FAILED = "TOOL_EXECUTION_FAILED"   
    PROTOCOL_PARSE_FAILED = "PROTOCOL_PARSE_FAILED"
    UNPARSED_TOOL_CALL = "UNPARSED_TOOL_CALL"    
    NO_FINAL_CONTENT = "NO_FINAL_CONTENT"
    OUTPUT_LIMIT = "OUTPUT_LIMIT"
    STORE_WRITE_FAILED = "STORE_WRITE_FAILED"
    RUN_TOTAL_TIMEOUT = "RUN_TOTAL_TIMEOUT"
    FIRST_DELTA_TIMEOUT = "FIRST_DELTA_TIMEOUT"
    STREAM_IDLE_TIMEOUT = "STREAM_IDLE_TIMEOUT"
    CANCELLED = "CANCELLED"
    AUTH_ERROR = "AUTH_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    PARAM_ERROR = "PARAM_ERROR"
    CONNECT_ERROR = "CONNECT_ERROR"
    NETWORK_ERROR = "NETWORK_ERROR"
    SERVER_ERROR = "SERVER_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ErrorCategory:
    """决定重试策略的分类（RetryPolicy 只认分类，不认具体异常类型）。"""

    TRANSIENT = "transient"          # 连接失败/临时网络/5xx：无内容时可重试
    TIMEOUT = "timeout"              # 首包/空闲/总时长超时
    RATE_LIMITED = "rate_limited"    # 429：尊重等待预算
    AUTH = "auth"                    # 401/403：不重试相同凭据，可切备
    PARAM = "param"                  # 400 类：立即失败，不切备掩盖
    PROTOCOL = "protocol"            # 响应结构损坏
    CANCELLED = "cancelled"          # 用户取消：永不重试
    INTERNAL = "internal"            # 本地代码/存储故障


@dataclass
class VastarError(Exception):
    """跨层传递的业务错误；message 不得包含 API key 或请求体原文密钥。"""

    code: str
    category: str
    message: str
    retryable: bool = False
    details: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "category": self.category,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }
