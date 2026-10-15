"""Vastar runtime component."""

from __future__ import annotations

from dataclasses import dataclass

from core.errors import ErrorCategory, ErrorCode, VastarError


class RetryAction:
    RETRY_SAME = "retry_same"      # 同 provider 等待后重试
    SWITCH = "switch"              # 切换备用 provider
    FAIL = "fail"                  # 明确失败


@dataclass
class AttemptState:
    provider: str
    attempt_index: int             # 当前 provider 上第几次尝试（1 起）
    used_fallback: bool
    retry_wait_used_s: float = 0.0


@dataclass
class RetryDecision:
    action: str
    wait_s: float = 0.0
    reason: str = ""
    next_provider: str | None = None


MAX_RETRIES_SAME_PROVIDER = 2   # 首次 + 2 重试 = 同 provider 最多 3 次
MAX_TOTAL_ATTEMPTS = 4   


class RetryPolicy:
    """按错误分类给出确定性决策；不使用 LLM 判断该不该重试（基线 §3）。"""

    def __init__(self, *, fallback_provider: str | None,
                 retry_wait_budget_s: float = 30.0,
                 base_wait_s: float = 1.0, max_wait_s: float = 8.0) -> None:
        self._fallback = fallback_provider
        self._wait_budget = retry_wait_budget_s
        self._base_wait = base_wait_s
        self._max_wait = max_wait_s

    def decide(self, error: VastarError, state: AttemptState) -> RetryDecision:
        if error.code == ErrorCode.UNPARSED_TOOL_CALL:
            return RetryDecision(RetryAction.FAIL, reason="未解析工具标记已拦截，不自动重试或切备掩盖")
        if error.category == ErrorCategory.CANCELLED:
            return RetryDecision(RetryAction.FAIL, reason="用户取消永不重试")

        # 总尝试次数（含切备）硬上限 4；备用 provider 只有 1 次机会，不得重试
        total_so_far = (1 + MAX_RETRIES_SAME_PROVIDER) if state.used_fallback else state.attempt_index
        can_retry_same = (
            not state.used_fallback
            and state.attempt_index <= MAX_RETRIES_SAME_PROVIDER
            and total_so_far + 1 <= MAX_TOTAL_ATTEMPTS
        )
        can_switch = (not state.used_fallback) and self._fallback is not None

        if error.category == ErrorCategory.PARAM:
            return RetryDecision(RetryAction.FAIL, reason="参数错误立即失败，不切备掩盖")

        if error.category == ErrorCategory.AUTH:
            if can_switch:
                return RetryDecision(
                    RetryAction.SWITCH, next_provider=self._fallback,
                    reason="鉴权失败不重试相同凭据，切备用 provider",
                )
            return RetryDecision(RetryAction.FAIL, reason="鉴权失败且无可用备用")

        if error.category == ErrorCategory.RATE_LIMITED:
            wait_s = self._wait_for(state)
            if can_retry_same and state.retry_wait_used_s < self._wait_budget and wait_s > 0:
                return RetryDecision(
                    RetryAction.RETRY_SAME, wait_s=wait_s,
                    reason=f"限流，等待 {wait_s:.0f}s 后重试（等待预算内）",
                )
            if can_switch:
                return RetryDecision(
                    RetryAction.SWITCH, next_provider=self._fallback,
                    reason="限流且等待预算/重试次数用尽，切备用",
                )
            return RetryDecision(RetryAction.FAIL, reason="限流且无法重试/切备")

        # TRANSIENT / TIMEOUT / PROTOCOL（无内容时）
        if error.category in (
            ErrorCategory.TRANSIENT, ErrorCategory.TIMEOUT, ErrorCategory.PROTOCOL,
        ):
            if can_retry_same:
                wait_s = self._wait_for(state)
                return RetryDecision(
                    RetryAction.RETRY_SAME, wait_s=wait_s,
                    reason=f"{error.code}，第 {state.attempt_index} 次尝试失败，等待 {wait_s:.0f}s 重试",
                )
            if can_switch:
                return RetryDecision(
                    RetryAction.SWITCH, next_provider=self._fallback,
                    reason="同 provider 重试次数用尽，切备用 provider",
                )
            return RetryDecision(
                RetryAction.FAIL,
                reason=f"重试与备用均已用尽（{error.code}）",
            )

        return RetryDecision(RetryAction.FAIL, reason=f"错误分类 {error.category} 不可重试")

    def _wait_for(self, state: AttemptState) -> float:
        wait = min(self._max_wait, self._base_wait * (2 ** max(0, state.attempt_index - 1)))
        remaining = self._wait_budget - state.retry_wait_used_s
        return max(0.0, min(wait, remaining))
