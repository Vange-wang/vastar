"""Vastar runtime component."""

from __future__ import annotations

import asyncio
import contextlib

from core.context import ContextResult
from core.errors import ErrorCategory, ErrorCode, VastarError
from core.events import EventType
from core.llm.base import LLMRequest, ProviderSpec
from core.llm.capabilities import contract_record
from core.run_state import (
    AttemptOutcome,
    RunState,
    SharedState,
    _latency,
    _request_hash,
)

# _merge_tool_deltas 随迁 stream_consume（V6 拆分），此处再导出兼容既有引用
#（tests/test_task26_fixes.py / test_task41_budget_gate.py 的导入路径不变）
from core.stream_consume import StreamConsumer, _merge_tool_deltas  # noqa: F401
from core.tools.registry import ToolsContext
from core.tools.round_trip import ToolRoundTripper


class AttemptRunner:
    """一次 Run 的尝试执行与收尾（持有共享执行上下文 SharedState）。"""

    def __init__(self, prepare_context_fn, tools: ToolsContext | None = None,
                 continuation=None) -> None:
        self._prepare = prepare_context_fn  # (RunRequest, ProviderSpec) -> ContextResult
        self._tools = tools                 # None = 工具能力关闭（default-closed，行为同阶段一）
        
        self._continuation = continuation
        
        self._consumer = StreamConsumer()
        # 回合内工具往返（方案 A）拆至 core/tools/round_trip.py（V6）；竞速逻辑注入复用
        self._round_tripper = (
            ToolRoundTripper(tools, self._consumer.race_consume) if tools is not None else None
        )

    async def attempt_once(self, s: SharedState, spec: ProviderSpec, attempt_id: str,
                            attempt_index: int, ctx_result: ContextResult
                            ) -> tuple[AttemptOutcome | None, VastarError | None]:
        """发起一次请求并消费其流。返回 (outcome, None)=流自然结束；
        (outcome, err)=失败——**outcome 不随错误丢弃**（B05/F07：断流时已产生的
        正文/思考/工具片段留在 outcome 内，由 _attempt_loop 按分类裁决保留或重试）；
        取消触发时 err.category=CANCELLED。"""
        request = s.request
        # B07：新 attempt 重建自己的工具证据（防重试/切备后沿用上一 attempt 的旧组）
        s.committed_messages = None
        tools_schema = None
        if self._tools is not None and self._tools.registry.enabled:
            tools_schema = self._tools.registry.to_openai_tools()
        llm_request = LLMRequest(
            provider=spec.name, model=spec.model,
            messages=ctx_result.snapshot.messages, params=dict(spec.params),
            timeouts=spec.timeouts, tools=tools_schema,
            meta={"run_id": request.run_id, "attempt_id": attempt_id},
        )
        outcome = AttemptOutcome()
        err = await self.stream_attempt(s, spec, llm_request, attempt_id, attempt_index,
                                        outcome)
        if err is not None:
            return outcome, err
        if (outcome.tool_calls and self._tools is not None
                and self._tools.registry.enabled):
            
            # enabled 为空（default-closed）不走往返 → 交给 classify_stream_end 拒绝（DoD 1 总闸）。
            try:
                outcome = await self._round_tripper.run(
                    s, spec, llm_request, attempt_id, attempt_index, outcome)
            except VastarError as err:
                return outcome, err   # F07：往返中已产出的内容随 outcome 上交裁决
        return outcome, None

    async def stream_attempt(self, s: SharedState, spec: ProviderSpec,
                             llm_request: LLMRequest, attempt_id: str, attempt_index: int,
                             outcome: AttemptOutcome,
                             extra_payload: dict | None = None) -> VastarError | None:
        """Vastar runtime component."""
        s.result.attempt_ids.append(attempt_id)
        _latency(s.latencies, "submit_to_request_sent", s.request.submitted_mono_ns)
        payload = {
            "provider": spec.name, "model": spec.model, "config_id": s.request.config_id,
            "context_id": f"{s.request.run_id}-ctx",
            "request_hash": _request_hash(spec.model, llm_request.params,
                                          llm_request.messages),
            "request_params": dict(llm_request.params),   # 快照（不含 key 与消息原文）
            "attempt_index": attempt_index,
        }
        if extra_payload:
            payload.update(extra_payload)   
        await s.emit(s.factory.build(EventType.ATTEMPT_STARTED, payload,
                                     attempt_id=attempt_id, request_epoch=attempt_index))
        await s.sm.transition(RunState.REQUESTING, "request_sent")
        try:
            await self._consumer.race_consume(s, spec, llm_request, attempt_index,
                                              attempt_id, outcome)
        except VastarError as err:
            return err
        finally:
            # B05/F07：断流帧同样留底（保留已产出内容的审计事实；成功路径行为不变）
            s.frames_by_attempt[attempt_id] = outcome.frames
        return None


    # ---- 结束分类与收尾 ---------------------------------------------------------------

    async def classify_stream_end(self, s: SharedState, outcome: AttemptOutcome,
                                   spec: ProviderSpec, ctx_result: ContextResult) -> bool:
        """流自然结束后的终局分类。返回 True 表示已达终态。"""
        
        
        if outcome.tool_calls:
            err = VastarError(
                code=ErrorCode.CAPABILITY_UNAVAILABLE, category=ErrorCategory.PROTOCOL,
                message="阶段一未启用工具：模型返回 tool_calls，已拒绝执行（零次工具执行）",
                details={"tool_call_count": len(outcome.tool_calls)})
            await self.record_error(s, err, s.result.attempt_ids[-1] if s.result.attempt_ids else "")
            await self.terminate(s, RunState.FAILED, ErrorCode.CAPABILITY_UNAVAILABLE)
            return True
        finish = outcome.finish_reason
        if finish == "needs_input":
            await self.terminate(s, RunState.INCOMPLETE, "needs_input")
            return True
        if finish == "input_budget_exceeded":
            # B06：工具续问/强制终答被预算闸门拦截（请求未发出）——如实收尾：
            # 已产出内容保留为部分结果，拦截详情与建议随 run.ended.warnings 可见。
            s.result.partial = bool(outcome.has_content or outcome.has_reasoning)
            await self.terminate(s, RunState.INCOMPLETE, "input_budget_exceeded")
            return True
        if finish == "length":
            
            # 共享 run_total_s 预算、闸门复查；预算用尽/闸门拦截/失败 → 落 INCOMPLETE）
            if self._continuation is not None and self._continuation.accepts(outcome):
                return await self._continuation.resume(s, spec, outcome, ctx_result, self)
            s.result.partial = True
            await self.terminate(s, RunState.INCOMPLETE, "output_limit")
            return True
        if finish is not None:
            if outcome.has_content:
                await self.terminate(s, RunState.COMPLETED, finish)
            else:
                s.result.partial = bool(outcome.has_reasoning)
                await self.terminate(s, RunState.INCOMPLETE, ErrorCode.NO_FINAL_CONTENT)
            return True
        if outcome.has_content or outcome.has_reasoning:
            s.result.partial = True
            await self.terminate(s, RunState.INCOMPLETE, "stream_closed_without_finish")
            return True
        return False

    async def record_error(self, s: SharedState, err: VastarError, attempt_id: str) -> None:
        s.errors.append(err.to_dict())
        s.result.errors = s.errors
        
        # （error_json 落库）；payload 键保留兼容。空串 = Run 级错误，不落具体 attempt。
        await s.emit(s.factory.build(EventType.ERROR, {
            **err.to_dict(), "attempt_id": attempt_id, "next_action": "decide_retry",
        }, attempt_id=attempt_id or None))

    async def fail_before_request(self, s: SharedState, err: VastarError) -> None:
        s.errors.append(err.to_dict())
        s.result.errors = s.errors
        await s.emit(s.factory.build(EventType.ERROR, {**err.to_dict(), "next_action": "fail"}))
        await self.terminate(s, RunState.FAILED, err.code)

    async def cancel_path(self, s: SharedState) -> None:
        await self.terminate(s, RunState.CANCELLED, "cancelled_by_user")

    async def retry_wait(self, s: SharedState, wait_s: float) -> bool:
        """等待重试间隔；返回 True 表示取消触发。"""
        await s.sm.transition(RunState.RETRY_WAIT, "retry_wait")
        wait_task = asyncio.create_task(asyncio.sleep(wait_s))
        cancel_task = asyncio.create_task(s.request.cancel_event.wait())
        done, _ = await asyncio.wait({wait_task, cancel_task},
                                     return_when=asyncio.FIRST_COMPLETED)
        for t in (wait_task, cancel_task):
            if not t.done():
                t.cancel()
        return cancel_task in done

    async def switch_provider(self, s: SharedState, spec: ProviderSpec, err: VastarError
                               ) -> tuple[ProviderSpec, ContextResult] | None:
        new_spec = s.request.fallback
        await s.sm.transition(RunState.SWITCHING, f"fallback_after_{err.code}")
        try:
            ctx_result = self._prepare(s.request, new_spec)
        except VastarError as budget_err:
            await self.record_error(s, budget_err, "")
            return None
        s.result.degraded = True
        s.result.degradation_reasons.append(f"provider_fallback:{spec.name}->{new_spec.name}")
        s.degraded_to = f"{new_spec.name}/{new_spec.model}"
        s.warnings.append(f"降级能力/价格声明：{contract_record(spec)} → {contract_record(new_spec)}；"
                          "共享 Run 预算继续累计，未验证能力不升级状态")
        return new_spec, ctx_result

    async def on_run_total_timeout(self, s: SharedState) -> None:
        err = VastarError(
            code=ErrorCode.RUN_TOTAL_TIMEOUT, category=ErrorCategory.TIMEOUT,
            message="Run 总时长超限，停止本次运行")
        if s.sm.terminal is not None:
            return
        s.errors.append(err.to_dict())
        s.result.errors = s.errors
        with contextlib.suppress(Exception):
            await s.emit(s.factory.build(EventType.ERROR, err.to_dict()))
            
            # :38 口径）——partial 旗标只在 classify 路径置位，流中超时够不到，
            # 故按 frames_by_attempt 里是否有真实正文判定。
            from core.runtime_delivery import evidence
            body, receipts = evidence(s)
            has_output = s.result.partial or bool(body.strip() or receipts or s.pending_terminal_text.strip())
            if has_output:
                await self.terminate(s, RunState.INCOMPLETE, "run_total_timeout")
            else:
                await self.terminate(s, RunState.FAILED, "run_total_timeout")

    async def terminate(self, s: SharedState, terminal: str, finish_reason: str) -> None:
        """收尾：终态 + run.ended 事件（含 RunResult、上下文快照、录制帧、延迟）。幂等。"""
        if s.sm.terminal is not None:
            return
        s.result.status = terminal.lower()
        s.result.finish_reason = finish_reason
        if terminal != RunState.COMPLETED and finish_reason != 'needs_input':
            from core.runtime_delivery import deliver_partial
            await deliver_partial(s, finish_reason)
        if s.sm.state != RunState.FINALIZING:
            await s.sm.transition(RunState.FINALIZING, f"finalize_{terminal.lower()}")
        _latency(s.latencies, "submit_to_terminal_result", s.request.submitted_mono_ns)
        payload = _run_ended_payload(s)
        if not s.ended_emitted:
            s.ended_emitted = True
            await s.emit(s.factory.build(EventType.RUN_ENDED, payload))
        await s.sm.transition(terminal, f"finalize_{terminal.lower()}")


def _run_ended_payload(s: SharedState) -> dict:
    """Vastar runtime component."""
    # 终态快照就是投影事实；不能等 runtime.run 返回后才补这些计数。
    s.result.model_calls = s.model_calls
    s.result.tool_rounds = s.tool_rounds
    s.result.tool_executions = s.tool_exec_count
    return {
        "result": dict(s.result.__dict__),  # B04：确定投影所需完整终态快照
        "status": s.result.status,
        "finish_reason": s.result.finish_reason,
        "answer_message_id": s.result.answer_message_id,
        "partial": s.result.partial,
        "context_outcome": s.result.context_outcome,
        "compression_report_id": s.result.compression_report_id,
        "degraded": s.result.degraded,
        "degraded_to": s.degraded_to,
        "degradation_reasons": s.result.degradation_reasons,
        "attempt_ids": s.result.attempt_ids,
        "usage_totals": s.result.usage_totals,
        "errors": s.result.errors,
        "warnings": s.warnings,
        "context_snapshot": s.result.context_record,
        "frames_by_attempt": s.frames_by_attempt,
        "latencies": s.latencies,
        
        "continuations": s.result.continuations,
        # B05：费用口径——发起请求数（model_calls）与 usage 到达数（usage_reports）分列；
        # usage_reports == 0 且 model_calls > 0 → 结束行标 token unknown（禁写 0）
        "model_calls": s.model_calls,
        "usage_reports": s.result.usage_reports,
        "call_records": s.result.call_records,
        "usage_status": s.result.usage_status,
        "cost_summary": s.result.cost_summary,
        "budget_summary": s.result.budget_summary,
        "max_output_tokens": (s.request.primary.params.get("max_tokens")
                              if s.request.primary else None),
        "run_total_s": s.run_total_s or None,
    }
