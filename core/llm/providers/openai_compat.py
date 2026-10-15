"""Vastar runtime component."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from core.errors import ErrorCategory, ErrorCode, VastarError
from core.llm.base import FrameKind, LLMProvider, LLMRequest, ProviderFrame
from core.llm.http_stream import BlockingSSESession
from core.llm.sse import SSEDecoder

DEFAULT_CONNECT_TIMEOUT_S = 10.0        
DEFAULT_FIRST_DELTA_TIMEOUT_S = 30.0    
DEFAULT_STREAM_IDLE_TIMEOUT_S = 60.0    


class OpenAICompatProvider(LLMProvider):
    """经 OpenAI 兼容 /chat/completions 流式端点工作的供应商适配器。"""

    name = "openai_compat"

    def __init__(self, *, provider_name: str, base_url: str, api_key: str,
                 chat_path: str = "/chat/completions") -> None:
        self.name = provider_name
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._chat_path = chat_path

    # ---- 参数映射（serialize）-------------------------------------------------

    def build_body(self, request: LLMRequest) -> dict:
        body: dict = {
            "model": request.model,
            "messages": request.messages,
            "stream": True,
        }
        body.update(request.params)
        if request.tools:
            body["tools"] = request.tools      # R19：标准 function-calling（tools + tool_calls）
        return self.amend_body(body, request)

    def amend_body(self, body: dict, request: LLMRequest) -> dict:
        """供应商专属参数钩子（子类覆盖）。"""
        return body

    # ---- 协议解析（parse）------------------------------------------------------

    def parse_chunk(self, obj: dict, chunk_index: int) -> list[ProviderFrame]:
        frames: list[ProviderFrame] = []
        choices = obj.get("choices") or []
        if not choices:
            usage = obj.get("usage")
            if usage:
                frames.append(ProviderFrame(
                    kind=FrameKind.USAGE, usage=dict(usage), source_chunk_index=chunk_index,
                ))
            else:
                frames.append(ProviderFrame(kind=FrameKind.HEARTBEAT, source_chunk_index=chunk_index))
            return frames

        choice = choices[0]
        delta = choice.get("delta") or {}

        # 两个字段分别检查、分别产生帧（固定顺序：思考 → 正文），不得互斥
        reasoning = delta.get("reasoning_content")
        if reasoning:
            frames.append(ProviderFrame(
                kind=FrameKind.REASONING, text=reasoning, source_chunk_index=chunk_index,
            ))
        content = delta.get("content")
        if content:
            frames.append(ProviderFrame(
                kind=FrameKind.CONTENT, text=content, source_chunk_index=chunk_index,
            ))
        tool_calls = delta.get("tool_calls")
        if tool_calls:
            frames.append(ProviderFrame(
                kind=FrameKind.CONTENT, text="", tool_calls=tool_calls,
                source_chunk_index=chunk_index,
            ))

        usage = obj.get("usage")
        if usage:
            frames.append(ProviderFrame(
                kind=FrameKind.USAGE, usage=dict(usage), source_chunk_index=chunk_index,
            ))

        finish_reason = choice.get("finish_reason")
        if finish_reason:
            frames.append(ProviderFrame(
                kind=FrameKind.FINISH, finish_reason=str(finish_reason),
                source_chunk_index=chunk_index,
            ))
        return frames

    # ---- 流式主路径 ------------------------------------------------------------

    async def stream(self, request: LLMRequest) -> AsyncIterator[ProviderFrame]:
        loop = asyncio.get_running_loop()
        timeouts = request.timeouts
        session = BlockingSSESession(loop)
        decoder = SSEDecoder()
        chunk_index = 0
        got_model_content = False
        produced_any = False

        session.start(
            url=self._base_url + self._chat_path,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
                "Accept": "text/event-stream",
            },
            body=self.build_body(request),
            connect_timeout_s=float(timeouts.get("connect_s", DEFAULT_CONNECT_TIMEOUT_S)),
            read_timeout_s=float(timeouts.get("stream_idle_s", DEFAULT_STREAM_IDLE_TIMEOUT_S)),
        )
        try:
            while True:
                first = not produced_any
                if first:
                    wait_s = float(timeouts.get("first_delta_s", DEFAULT_FIRST_DELTA_TIMEOUT_S))
                else:
                    wait_s = float(timeouts.get("stream_idle_s", DEFAULT_STREAM_IDLE_TIMEOUT_S))
                try:
                    kind, payload = await asyncio.wait_for(session.queue.get(), timeout=wait_s)
                except TimeoutError as exc:
                    if first and not got_model_content:
                        raise VastarError(
                            code=ErrorCode.FIRST_DELTA_TIMEOUT,
                            category=ErrorCategory.TIMEOUT,
                            message="等待首个模型内容超时（心跳不刷新该计时）",
                            retryable=True,
                        ) from exc
                    raise VastarError(
                        code=ErrorCode.STREAM_IDLE_TIMEOUT,
                        category=ErrorCategory.TIMEOUT,
                        message="流式输出空闲超时", retryable=True,
                    ) from exc

                if kind == "done":
                    break
                if kind == "error":
                    raise payload  # 已是 VastarError
                if kind == "heartbeat":
                    yield ProviderFrame(kind=FrameKind.HEARTBEAT, source_chunk_index=chunk_index)
                    chunk_index += 1
                    continue

                # 原始字节块 → SSE 解码（跨块行拼接）→ 逐事件解析
                chunk_frames: list[ProviderFrame] = []

                def _on_data(payload_str: str,
                             _idx: int = chunk_index,
                             _out: list[ProviderFrame] = chunk_frames) -> None:
                    _out.extend(self.parse_chunk(SSEDecoder.parse_json(payload_str), _idx))

                def _on_heartbeat(_idx: int = chunk_index,
                                  _out: list[ProviderFrame] = chunk_frames) -> None:
                    _out.append(ProviderFrame(
                        kind=FrameKind.HEARTBEAT, source_chunk_index=_idx,
                    ))

                decoder.feed(bytes(payload), on_data=_on_data, on_heartbeat=_on_heartbeat)
                chunk_index += 1
                for frame in chunk_frames:
                    if frame.kind in (FrameKind.REASONING, FrameKind.CONTENT):
                        got_model_content = True
                    if frame.kind != FrameKind.HEARTBEAT:   
                        produced_any = True
                    yield frame
                if decoder.done:
                    break
        finally:
            session.close()
