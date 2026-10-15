"""Reject unparsed control markup before content reaches rendering or persistence.

Reserved XML-like tool_call(s)/function/parameter tags (including partial tags
at stream end) are diagnostic data, never executable calls or answer text.
Private dialects are checked structurally before this legacy reserved-tag rule.
Literal documentation can use HTML-escaped spellings; ordinary HTML is unchanged.
"""
import asyncio
import re
from contextlib import aclosing
from dataclasses import replace

from core.errors import ErrorCategory, ErrorCode, VastarError
from core.llm.base import FrameKind, ProviderFrame
from core.llm.private_markup import PrivateMarkupGuard

_NAMES = ("tool_call", "tool_calls", "function", "parameter")
_MARKER = re.compile(r"<\s*/?\s*(?:tool_calls?|function|parameter)[\s/>=]", re.I)


class OutputGuard:
    """Hold possible control markup; ordinary answer text remains streaming."""

    def __init__(self):
        self.pending = ""
        self.raw = []
        self.blocked = False
        self.private = PrivateMarkupGuard()

    def feed(self, text: str) -> str:
        self.raw.append(text)
        if self.blocked:
            return ""
        visible = self._scan(self.private.feed(text))
        self.blocked |= self.private.blocked
        return visible

    def _scan(self, text):
        visible = []
        for char in text:
            if not self.pending and char != "<":
                visible.append(char)
                continue
            self.pending += char
            if _MARKER.match(self.pending):
                self.blocked = True
                break
            candidate = self.pending[1:].lstrip().removeprefix("/").lstrip().lower()
            if any(name.startswith(candidate) for name in _NAMES):
                continue
            # A second '<' can begin a marker even after a nonmatching prefix.
            restart = self.pending.endswith("<")
            visible.append(self.pending[:-1] if restart else self.pending)
            self.pending = "<" if restart else ""
        return "".join(visible)

    def tail(self) -> str:
        extra = self._scan(self.private.tail()) if not self.blocked else ""
        self.blocked |= self.private.blocked
        if self.pending.strip() not in ("", "<", "</"):
            self.blocked = True
        text, self.pending = self.pending, ""
        return "" if self.blocked else extra + text

    def check(self) -> None:
        if self.blocked:
            raise VastarError(
                ErrorCode.UNPARSED_TOOL_CALL, ErrorCategory.PROTOCOL,
                "模型输出了无法解析的工具调用；已阻止作为答案交付，未执行该文本中的调用。",
                retryable=False, details={"raw_output": "".join(self.raw),
                                          "diagnostic_only": True})


async def guarded_output(frames, *, timed_out=None):
    """Keep usage/error/cancel semantics; retain rejected text in call diagnostics."""
    guard = OutputGuard()
    last_index = 0
    try:
        async with aclosing(frames):
            async for frame in frames:
                if frame.kind == FrameKind.CONTENT and not frame.tool_calls:
                    last_index = frame.source_chunk_index
                    text = guard.feed(frame.text)
                    if text:
                        yield replace(frame, text=text)
                elif frame.kind == FrameKind.FINISH:
                    tail = guard.tail()
                    if tail:
                        yield ProviderFrame(kind=FrameKind.CONTENT, text=tail,
                                            source_chunk_index=last_index)
                    if not guard.blocked:
                        yield frame
                elif not (guard.blocked and frame.kind == FrameKind.CONTENT):
                    yield frame
    except asyncio.CancelledError:
        # Only the call's expired deadline can supersede cancellation. User
        # cancellation and unrelated task shutdown retain their original meaning.
        if timed_out is not None and timed_out():
            guard.tail()
            guard.check()
        raise
    except Exception:
        guard.tail()
        guard.check()  # Detected control markup must never enter retry/fallback.
        raise
    tail = guard.tail()
    if tail:
        yield ProviderFrame(kind=FrameKind.CONTENT, text=tail,
                            source_chunk_index=last_index)
    guard.check()
