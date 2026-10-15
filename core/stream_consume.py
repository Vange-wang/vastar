"""Vastar runtime component."""

from __future__ import annotations

import asyncio
import contextlib

from core.errors import ErrorCategory, ErrorCode, VastarError
from core.events import EventType
from core.harness.model_calls import request_for
from core.harness.model_gateway import ModelGateway
from core.llm.base import FrameKind, LLMRequest, ProviderSpec
from core.run_state import (
    AttemptOutcome,
    RunState,
    SharedState,
    _latency,
)


def _merge_tool_deltas(existing: list | None, deltas: list) -> list:
    """Vastar runtime component."""
    merged = list(existing or [])
    for delta in deltas:
        idx = int(delta.get("index", len(merged)))
        while len(merged) <= idx:
            merged.append({})
        slot = merged[idx]
        if delta.get("id"):
            slot["id"] = delta["id"]
        slot.setdefault("type", delta.get("type", "function"))
        fn = slot.setdefault("function", {})
        piece = delta.get("function") or {}
        if piece.get("name"):
            fn["name"] = piece["name"]
        if piece.get("arguments"):
            fn["arguments"] = fn.get("arguments", "") + piece["arguments"]
    for idx, slot in enumerate(merged):
        if not slot.get("id"):
            slot["id"] = f"call_{idx}"
    return merged


class StreamConsumer:
    """provider 流消费体（无 Run 级自持状态；一切经 SharedState 传入）。

    race_consume 由 AttemptRunner 注入 ToolRoundTripper：工具续问/强制终答与
    首跳共用同一"消费 vs 取消"竞速语义。
    """

    def __init__(self):
        self.gateway = ModelGateway()

    async def race_consume(self, s: SharedState, spec: ProviderSpec,
                           llm_request: LLMRequest, attempt_index: int, attempt_id: str,
                           outcome: AttemptOutcome) -> None:
        """消费 provider 流并与取消事件竞速；取消/流异常按契约抛 VastarError。"""
        consume = asyncio.create_task(
            self._consume(s, spec, llm_request, attempt_index, attempt_id, outcome))
        cancel_task = asyncio.create_task(s.request.cancel_event.wait())
        try:
            await asyncio.wait({consume, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
            if not consume.done():
                cancel_task.cancel()
                consume.cancel()
                with contextlib.suppress(asyncio.CancelledError, VastarError):
                    await consume
                raise VastarError(
                    code=ErrorCode.CANCELLED, category=ErrorCategory.CANCELLED,
                    message="用户取消（流消费已停止）")
            cancel_task.cancel()
            err = consume.exception()
            if err is not None:
                if s.request.cancel_event.is_set():
                    raise VastarError(
                        code=ErrorCode.CANCELLED, category=ErrorCategory.CANCELLED,
                        message="用户取消（与流错误同时到达，取消优先）")
                raise err
        finally:
            for task in (consume, cancel_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(consume, cancel_task, return_exceptions=True)

    async def _consume(self, s: SharedState, spec: ProviderSpec, llm_request: LLMRequest,
                       attempt_index: int, attempt_id: str, outcome: AttemptOutcome) -> None:
        in_stream = False
        call = request_for(s, spec, llm_request, attempt_id)
        async with contextlib.aclosing(self.gateway.stream(s, call)) as frames:
            async for frame in frames:
                if frame.kind == FrameKind.HEARTBEAT:
                    continue
                outcome.frames.append({
                    "kind": frame.kind, "text": frame.text,
                    "finish_reason": frame.finish_reason, "usage": frame.usage,
                    "tool_calls": frame.tool_calls,
                    "source_chunk_index": frame.source_chunk_index,
                })
                if frame.kind == FrameKind.USAGE and frame.usage:
                    await s.emit(s.factory.build(EventType.USAGE_REPORTED, {
                        **s.call_ledger[call.call_id].actual_usage,
                        "call_id": call.call_id, "usage_status": s.call_ledger[call.call_id].usage_status,
                        "source": "api_usage",
                    }, channel="metrics", attempt_id=attempt_id, request_epoch=attempt_index))
                    continue
                if frame.kind == FrameKind.REASONING:
                    if not in_stream:
                        in_stream = True
                        if s.sm.state != RunState.STREAMING:
                            await s.sm.transition(RunState.STREAMING, "first_model_frame")
                    outcome.has_reasoning = True
                    _latency(s.latencies, "submit_to_first_reasoning_visible",
                             s.request.submitted_mono_ns)
                    await s.emit(s.factory.build(EventType.REASONING_DELTA, {
                        "text": frame.text, "source_chunk_index": frame.source_chunk_index,
                    }, attempt_id=attempt_id, request_epoch=attempt_index))
                elif frame.kind == FrameKind.CONTENT:
                    if frame.tool_calls:
                        # 流式 tool_calls 跨片段（R19）：按 index 累积，id 原样字符串
                        outcome.tool_calls = _merge_tool_deltas(outcome.tool_calls,
                                                                frame.tool_calls)
                        continue
                    if not in_stream:
                        in_stream = True
                        if s.sm.state != RunState.STREAMING:
                            await s.sm.transition(RunState.STREAMING, "first_model_frame")
                    outcome.has_content = True
                    _latency(s.latencies, "submit_to_first_content_visible",
                             s.request.submitted_mono_ns)
                    await s.emit(s.factory.build(EventType.CONTENT_DELTA, {
                        "text": frame.text, "source_chunk_index": frame.source_chunk_index,
                    }, attempt_id=attempt_id, request_epoch=attempt_index))
                elif frame.kind == FrameKind.FINISH:
                    outcome.finish_reason = frame.finish_reason
