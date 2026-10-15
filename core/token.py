"""Vastar runtime component."""

from __future__ import annotations

import math
from dataclasses import dataclass

from core.errors import ErrorCategory, ErrorCode, VastarError

GUARANTEE_ESTIMATED = "estimated"
GUARANTEE_BOUNDED = "bounded"
GUARANTEE_EXACT = "exact"

TOKENIZER_VERSION = "heuristic_cjk_v1"
PROVIDER_CONTRACT_VERSION = "openai-compat-stream-v1"

OTHER_CHARS_PER_TOKEN = 3.5   
_CJK_RANGES = (
    (0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0x20000, 0x2A6DF),
    (0xF900, 0xFAFF), (0x3000, 0x303F), (0xFF00, 0xFFEF),
)


@dataclass
class TokenMeasurement:
    estimated_tokens: int
    upper_bound_tokens: int | None
    method: str
    tokenizer_version: str
    provider_contract_version: str
    guarantee: str

    @property
    def has_trusted_upper_bound(self) -> bool:
        return self.guarantee in (GUARANTEE_BOUNDED, GUARANTEE_EXACT) and (
            self.upper_bound_tokens is not None
        )


@dataclass
class BudgetProfile:
    """当前 provider 的预算参数（由 ContextAssembler 从配置推导）。"""

    provider: str
    model: str
    product_input_limit: int | None
    context_window: int
    reserve_output_tokens: int

    @property
    def input_budget(self) -> int:
        if self.product_input_limit is None:
            return self.context_window - self.reserve_output_tokens
        return min(
            self.product_input_limit,
            self.context_window - self.reserve_output_tokens,
        )


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def estimate_text_tokens(text: str) -> int:
    """启发式估算：CJK 按字计权、非 CJK 按字符长度折算。只作预警，不作上界证明。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if _is_cjk(ch))
    other = len(text) - cjk
    return math.ceil(cjk + other / OTHER_CHARS_PER_TOKEN)


def measure(texts: list[str], *, safety_margin: float = 0.0) -> TokenMeasurement:
    """对一组文本做启发式估算。阶段一 guarantee 恒为 estimated（无可信上界）。"""
    raw = sum(estimate_text_tokens(t) for t in texts)
    estimated = math.ceil(raw * (1.0 + safety_margin)) if safety_margin else raw
    return TokenMeasurement(
        estimated_tokens=estimated,
        upper_bound_tokens=None,
        method="heuristic_cjk_v1",
        tokenizer_version=TOKENIZER_VERSION,
        provider_contract_version=PROVIDER_CONTRACT_VERSION,
        guarantee=GUARANTEE_ESTIMATED,
    )


@dataclass
class GateDecision:
    allowed: bool
    estimated_tokens: int
    gate_tokens: int          # G = ceil(I × (1 + margin))
    budget: int               # B
    guarantee: str
    error: VastarError | None = None
    note: str = ""


def check_gate(
    measurement: TokenMeasurement,
    profile: BudgetProfile,
    *,
    safety_margin_ratio: float = 0.1,
    strict: bool = False,
    strict_error_code: str = ErrorCode.TOKEN_GUARANTEE_UNAVAILABLE,
) -> GateDecision:
    """预算闸门。

    - 正常路径：估算 × margin 与预算比较；超限 → CONTEXT_BUDGET_EXCEEDED（显式失败，
      调用方不得静默截断后重试）。
    - strict=True：要求可信上界；只有估算时拒绝调用并返回 TOKEN_GUARANTEE_UNAVAILABLE。
      （阶段一启发式估算永远不满足该要求。）
    允许通过时 guarantee 如实标注，绝不声称 bounded/exact。
    """
    if strict and not measurement.has_trusted_upper_bound:
        return GateDecision(
            allowed=False,
            estimated_tokens=measurement.estimated_tokens,
            gate_tokens=0,
            budget=profile.input_budget,
            guarantee=measurement.guarantee,
            error=VastarError(
                code=strict_error_code,
                category=ErrorCategory.INTERNAL,
                message="严格模式要求可信 token 上界，当前仅有启发式估算，已拒绝调用"
                        "（估算只作预警，不证明硬上界）",
                details={"guarantee": measurement.guarantee,
                         "tokenizer_version": measurement.tokenizer_version},
            ),
            note="strict 模式：无可信上界，诚实拒绝",
        )
    gate_tokens = math.ceil(measurement.estimated_tokens * (1.0 + safety_margin_ratio))
    budget = profile.input_budget
    if gate_tokens > budget:
        return GateDecision(
            allowed=False,
            estimated_tokens=measurement.estimated_tokens,
            gate_tokens=gate_tokens,
            budget=budget,
            guarantee=measurement.guarantee,
            error=VastarError(
                code=ErrorCode.CONTEXT_BUDGET_EXCEEDED,
                category=ErrorCategory.INTERNAL,
                message=(
                    f"上下文预算超限：估算 {measurement.estimated_tokens} × "
                    f"{1 + safety_margin_ratio:.2f} = {gate_tokens} > 预算 {budget}"
                ),
                details={
                    "provider": profile.provider,
                    "budget": budget,
                    "estimated_tokens": measurement.estimated_tokens,
                    "gate_tokens": gate_tokens,
                },
            ),
            note="估算超预算（口径：估算未超预算才放行，非硬上界保证）",
        )
    return GateDecision(
        allowed=True,
        estimated_tokens=measurement.estimated_tokens,
        gate_tokens=gate_tokens,
        budget=budget,
        guarantee=measurement.guarantee,
        note="估算未超预算（非硬上界保证）",
    )
