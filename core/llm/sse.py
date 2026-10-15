"""SSE（Server-Sent Events）增量解码器。

OpenAI 兼容流式响应的通用解析：
- "data: {...}" → 交给回调；"data: [DONE]" → 结束哨兵；
- ":" 开头的注释行与心跳空行 → heartbeat（不携带模型内容，不得刷新 first_delta 计时）；
- 非法 JSON → PROTOCOL_PARSE_FAILED。
"""

from __future__ import annotations

import json
from collections.abc import Callable

from core.errors import ErrorCategory, ErrorCode, VastarError


class SSEDecoder:
    """增量喂入 bytes，产出完整 SSE 事件。跨 chunk 的行拼接由本类负责。"""

    def __init__(self) -> None:
        self._buffer = b""
        self._done = False

    @property
    def done(self) -> bool:
        return self._done

    def feed(
        self,
        chunk: bytes,
        *,
        on_data: Callable[[str], None],
        on_heartbeat: Callable[[], None] | None = None,
    ) -> None:
        self._buffer += chunk
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            self._handle_line(line.decode("utf-8", errors="replace"),
                              on_data=on_data, on_heartbeat=on_heartbeat)
            if self._done:
                return

    def _handle_line(
        self,
        line: str,
        *,
        on_data: Callable[[str], None],
        on_heartbeat: Callable[[], None] | None,
    ) -> None:
        line = line.rstrip("\r")
        if not line or line.startswith(":"):
            if on_heartbeat is not None:
                on_heartbeat()
            return
        if not line.startswith("data:"):
            # OpenAI 兼容流只允许 data 行；事件行不出现，出现即协议异常
            raise VastarError(
                code=ErrorCode.PROTOCOL_PARSE_FAILED,
                category=ErrorCategory.PROTOCOL,
                message=f"SSE 行不以 data: 开头（前 32 字符：{line[:32]!r}）",
            )
        payload = line[len("data:"):].strip()
        if payload == "[DONE]":
            self._done = True
            return
        on_data(payload)

    @staticmethod
    def parse_json(payload: str) -> dict:
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise VastarError(
                code=ErrorCode.PROTOCOL_PARSE_FAILED,
                category=ErrorCategory.PROTOCOL,
                message=f"SSE data 不是合法 JSON（{exc.msg} @ pos {exc.pos}）",
            ) from exc
        if not isinstance(obj, dict):
            raise VastarError(
                code=ErrorCode.PROTOCOL_PARSE_FAILED,
                category=ErrorCategory.PROTOCOL,
                message="SSE data 不是 JSON 对象",
            )
        return obj
