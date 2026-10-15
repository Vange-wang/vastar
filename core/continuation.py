"""Vastar runtime component."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.errors import ErrorCategory
from core.harness.preflight import context_decision
from core.llm.base import FrameKind, LLMRequest
from core.run_state import AttemptOutcome, RunState

if TYPE_CHECKING:  # 类型标注用（避免运行期循环依赖）
    from core.context import ContextResult
    from core.llm.base import ProviderSpec
    from core.run_state import SharedState
    from core.runtime_attempt import AttemptRunner


def _content_text(frames: list[dict]) -> str:
    """从帧序列提取正文累计（tool_calls 帧 text 恒为空，天然剔除）。"""
    return "".join(f["text"] for f in frames
                   if f["kind"] == FrameKind.CONTENT and f["text"])


def _tool_evidence_text(messages: list[dict]) -> str:
    """协议适配：把成组 assistant(tool_calls)+tool replies 显式转换为文本证据（B07）。

    保留每个工具观察的工具名与内容原文（不丢弃、不改写），以显式标记区块呈现。
    """
    lines = ["[工具观察证据（协议适配：本供应商不接受历史 tool 消息，以下为等值文本证据）]"]
    names: dict[str, str] = {}   # tool_call_id → 工具名
    for m in messages:
        for tc in m.get("tool_calls") or []:
            names[str(tc.get("id") or "")] = str((tc.get("function") or {}).get("name", "?"))
        if m.get("role") == "tool":
            name = names.get(str(m.get("tool_call_id") or ""), "?")
            lines.append(f"<{name}> {m.get('content') or ''}")
    return "\n".join(lines)


class ContinuationRunner:
    """撞顶自动续写循环（策略与编排；流消费复用 AttemptRunner.stream_attempt）。"""

    def __init__(self, *, enabled: bool = True, max_count: int | None = 2) -> None:
        self._enabled = enabled
        from core.limits import optional_limit
        self._max_count = optional_limit(max_count, integer=True)

    @property
    def max_count(self) -> int | None:
        return self._max_count

    def accepts(self, outcome: AttemptOutcome) -> bool:
        """本 outcome 是否应进入续写：启用 + 撞顶 + 已有正文（空前缀续写无意义）。"""
        return (self._enabled and outcome.finish_reason == "length" and outcome.has_content)

    async def resume(self, s: SharedState, spec: ProviderSpec, outcome: AttemptOutcome,
                     ctx_result: ContextResult, runner: AttemptRunner) -> bool:
        """续写循环；返回 True = 已落定终态（调用方不得再 terminate）。"""
        accumulated = _content_text(outcome.frames)
        while self._max_count is None or s.result.continuations < self._max_count:
            request = self._build_request(s, spec, ctx_result, accumulated)
            gate_note = self._check_prefix_budget(s, spec, request)
            if gate_note is not None:
                s.warnings.append(gate_note)   # 如实标注：预算不足，停止续写（V2）
                break
            n = s.result.continuations + 1
            attempt_id = f"{s.request.run_id}-c{n}"
            await s.sm.transition(RunState.CONTINUING, f"continuation_{n}_of_{self._max_count}")
            next_outcome = AttemptOutcome()
            err = await runner.stream_attempt(
                s, spec, request, attempt_id, len(s.result.attempt_ids) + 1, next_outcome,
                extra_payload={"kind": "continuation", "continuation_index": n,
                               "continuation_max": self._max_count})
            if err is not None:
                if err.category == ErrorCategory.CANCELLED:
                    await runner.cancel_path(s)
                    return True
                # 续写失败不自动重试：已输出内容保留，如实收尾
                await runner.record_error(s, err, attempt_id)
                break
            s.result.continuations = n
            accumulated += _content_text(next_outcome.frames)
            if next_outcome.finish_reason == "length":
                continue                     # 仍撞顶：预算内再续
            if next_outcome.finish_reason is None:
                s.result.partial = True      # 续写流无结束原因：如实标注
                await runner.terminate(s, RunState.INCOMPLETE, "stream_closed_without_finish")
                return True
            await runner.terminate(s, RunState.COMPLETED, next_outcome.finish_reason)
            return True
        # 预算耗尽（或闸门/失败中断）仍撞顶 → 明示 + 手动 /continue 兜底
        s.result.partial = True
        await runner.terminate(s, RunState.INCOMPLETE, "output_limit")
        return True

    def _build_request(self, s: SharedState, spec: ProviderSpec,
                       ctx_result: ContextResult, prefix: str) -> LLMRequest:
        """Vastar runtime component."""
        base = list(s.committed_messages or ctx_result.snapshot.messages)
        if (any(m.get("role") == "tool" or m.get("tool_calls") for m in base)
                and not getattr(spec.provider, "supports_tool_messages_without_tools", True)):
            evidence = _tool_evidence_text(base)
            base = [m for m in base
                    if m.get("role") != "tool" and not m.get("tool_calls")]
            base.append({"role": "user", "content": evidence})
            s.warnings.append(
                "协议适配：本供应商不接受无 tools 参数的历史 tool 消息，"
                "已显式转换为文本证据保留（未丢弃任何工具观察）")
        messages = base + [{"role": "assistant", "content": prefix}]
        return LLMRequest(
            provider=spec.name, model=spec.model,
            messages=messages, params=dict(spec.params),
            timeouts=spec.timeouts,   # 共享 run_total_s 由 runtime 层 asyncio.timeout 承担
            meta={"run_id": s.request.run_id, "continuation": True},
        )

    def _check_prefix_budget(self, s: SharedState, spec: ProviderSpec,
                             request: LLMRequest) -> str | None:
        """续写请求整体计入输入预算复查（V2：超限即停并返回警示文案，不静默硬发）。

        B07：计量口径与 B06 工具续问同源（`_request_texts`——工具观察原文与
        tool_calls JSON 如实入账），前缀与历史 tool 消息一并计入。
        返回 None = 预算内可续写；否则返回警示文案（写入 s.warnings 随 run.ended 可见）。
        """
        gate = context_decision(s, spec, request)
        if gate.allowed:
            return None
        return (f"续写中止：已输出内容计入输入后超预算（估算 {gate.gate_tokens} > "
                f"预算 {gate.budget}），已保留已输出部分")
