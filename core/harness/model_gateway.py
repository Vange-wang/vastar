"""唯一供应商调用边界：preflight → 调用意图 → 适配器流 → 按 call 归账。"""
import asyncio
import hashlib
import json
from contextlib import aclosing
from dataclasses import asdict, replace

from core.budget_topup.runtime import model_timeout
from core.errors import ErrorCategory, ErrorCode, VastarError
from core.events import EventType
from core.harness.call_accounting import calculate_cost, record_usage, sync_run
from core.harness.model_calls import ModelCallRequest, ModelCallResult
from core.harness.preflight import (
    capability_check,
    check_run_budget,
    context_decision,
    estimate_components,
)
from core.llm.base import FrameKind
from core.llm.capabilities import contract_record
from core.llm.final_output import safe_terminal, terminal_output
from core.llm.output_guard import OutputGuard, guarded_output

__all__ = ["ModelGateway", "ModelCallRequest", "ModelCallResult"]


async def _audit_run_budget_rejection(s, call, error):
    if call.entry not in ("follow_up", "final_answer"):
        return
    detail = f"请求未发出（INPUT_BUDGET_EXCEEDED）：{error}"
    await s.emit(s.factory.build(EventType.TOOL_REJECTED, {
        "tool": "<next_round>", "reason": "input_budget_exceeded",
        "code": ErrorCode.INPUT_BUDGET_EXCEEDED, "detail": detail, "source": "gate",
        "call_id": call.call_id, "entry": call.entry,
    }, attempt_id=call.attempt_id))
    s.warnings.append(detail)


class ModelGateway:
    async def preflight(self, s, call, result):
        from memory.forget_runtime import check_request
        check_request(s.request)
        capability_check(call)
        result.estimated_usage = estimate_components(s, call.request)
        result.estimated_usage["components"]["output_reserve"] = int(
            call.request.params.get("max_tokens", call.spec.profile.reserve_output_tokens))
        if call.budget_policy is not None:
            blocked = await call.budget_policy()
            if blocked:
                raise VastarError(ErrorCode.INPUT_BUDGET_EXCEEDED, ErrorCategory.PARAM,
                                  "请求未发出：INPUT_BUDGET_EXCEEDED")
        else:
            decision = context_decision(s, call.spec, call.request)
            if not decision.allowed:
                error = decision.error
                if error.code == ErrorCode.CONTEXT_BUDGET_EXCEEDED:
                    error.code = ErrorCode.INPUT_BUDGET_EXCEEDED
                raise error
        try:
            return check_run_budget(s, result.estimated_usage)
        except VastarError as error:
            await _audit_run_budget_rejection(s, call, error)
            raise

    async def stream(self, s, call):
        old = s.call_ledger.get(call.call_id)
        if old is not None:
            if old.fingerprint != call.fingerprint():
                raise VastarError(ErrorCode.PARAM_ERROR, ErrorCategory.PARAM, "同 call_id 请求内容冲突")
            if call.entry not in old.entries_seen:
                old.entries_seen.append(call.entry)
            sync_run(s)
            return  # 幂等重入不重发供应商、不重复播内容/usage；结果在共享 ledger 可查。
        result = ModelCallResult(call.call_id, call.attempt_id, call.context_ref,
                                 call.spec.name, call.spec.model, call.entry, call.fingerprint(),
                                 pricing=dict(call.spec.pricing), entries_seen=[call.entry])
        s.call_ledger[call.call_id] = result  # 首个 await 前登记，跨入口并发也只有一个 owner。
        try:
            await self._admit(s, call, result)
            from memory.forget_runtime import check_request
            check_request(s.request)
            request = replace(call.request, meta={k: v for k, v in call.request.meta.items()
                                                  if not k.startswith("_")})
            request.meta.update(call_id=call.call_id, context_ref=call.context_ref, attempt_id=call.attempt_id)
            timeout = model_timeout(s.request, call.spec)
            finished = False
            deadline = asyncio.timeout(timeout)
            frames = self._output(s, call, request, result, deadline)
            async with deadline, aclosing(frames) as frames:
                async for frame in frames:
                    check_request(s.request)
                    finished = finished or frame.kind == FrameKind.FINISH
                    if frame.kind == FrameKind.USAGE:
                        if not frame.usage or not record_usage(result, frame.usage):
                            continue
                        sync_run(s)
                    yield frame
            result.status = "completed" if finished else "incomplete"
        except asyncio.CancelledError:
            result.status = "cancelled"
            result.error = VastarError(ErrorCode.CANCELLED, ErrorCategory.CANCELLED, "模型调用已取消").to_dict()
            raise
        except TimeoutError as exc:
            error = VastarError("MODEL_CALL_TIMEOUT", ErrorCategory.TIMEOUT, "模型调用请求超时", retryable=True)
            result.status, result.error = "timeout", error.to_dict()
            raise error from exc
        except VastarError as exc:
            result.status = ("cancelled" if exc.category == ErrorCategory.CANCELLED else
                             "timeout" if exc.category == ErrorCategory.TIMEOUT else
                             "failed" if result.started else "rejected")
            result.error = exc.to_dict()
            raise
        except GeneratorExit:
            result.status = "cancelled"
            raise
        except Exception as exc:
            error = VastarError(ErrorCode.INTERNAL_ERROR, ErrorCategory.INTERNAL,
                                "模型调用边界发生未分类异常", details={"exception_type": type(exc).__name__})
            result.status, result.error = "failed", error.to_dict()
            raise error from exc
        finally:
            calculate_cost(result)
            sync_run(s)
            await s.emit(s.factory.build(EventType.MODEL_CALL_FINISHED, asdict(result),
                                          channel="metrics", attempt_id=call.attempt_id))

    async def _intent(self, s, call, result):
        await s.emit(s.factory.build(EventType.MODEL_CALL_INTENT, {
            "call_id": call.call_id, "attempt_id": call.attempt_id, "entry": call.entry,
            "context_ref": call.context_ref, "estimated_usage": result.estimated_usage,
            "contract": contract_record(call.spec),
            "context": {"message_hashes": [hashlib.sha256(json.dumps(m, sort_keys=True,
                        ensure_ascii=False).encode()).hexdigest() for m in call.request.messages],
                        "request_hash": call.fingerprint()},  # 不把敏感工具观察原文写入审计。
        }, channel="metrics", attempt_id=call.attempt_id))

    def _output(self, s, call, request, result, deadline):
        frames = call.spec.provider.stream(request)
        if call.entry != 'final_answer' or request.tools is not None:
            return guarded_output(frames,
                timed_out=lambda: deadline.expired() and not s.request.cancel_event.is_set())

        async def rejected(error):
            result.error = error.to_dict()
            s.warnings.append('无工具终答的调用已拒绝执行；控制标记未进入答案。')
            await s.emit(s.factory.build(EventType.TOOL_REJECTED, {
                'tool': '<unparsed>', 'reason': 'final_output_sanitized', 'executed': False,
                'detail': '无工具终答的调用未执行，保留可交付正文。', 'source': 'model',
            }, attempt_id=call.attempt_id))

        async def interrupted(raw, native_calls):
            try:
                text, error = safe_terminal(raw, native_calls)
            except VastarError as error:
                guard = OutputGuard()
                text = guard.feed(raw)  # Only the already validated prefix survives.
                await rejected(error)
            else:
                if error:
                    await rejected(error)
            s.pending_terminal_text += text

        return terminal_output(frames, rejected, interrupted)

    async def _admit(self, s, call, result):
        async with s.call_admission_lock:
            required = await self.preflight(s, call, result)
            if s.request.cancel_event.is_set():
                raise VastarError(ErrorCode.CANCELLED, ErrorCategory.CANCELLED, "派发前取消")
            await self._intent(s, call, result)
            result.started, result.status = True, "streaming"
            s.call_budget["estimated_tokens"] += required
            sync_run(s)
