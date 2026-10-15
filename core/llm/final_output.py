"""A30: no-tool terminal responses retain prose, with calls diagnostic-only.

The normal OutputGuard is unchanged. A second guard validates stripped terminal
text before any CONTENT_DELTA; tool bodies/arguments are never interpreted.
"""
import re
from contextlib import aclosing
from dataclasses import replace

from core.errors import ErrorCategory, ErrorCode, VastarError
from core.llm.base import FrameKind, ProviderFrame
from core.llm.output_guard import OutputGuard
from core.llm.private_markup import CALL_ROLES, INVOKE_ROLES, PARAMETER_ROLES, PrivateMarkupGuard

_TAG = re.compile(r'<[^<>]*>')
_LEGACY = re.compile(r'<\s*(/?)\s*(tool_calls?|function|parameter|next_round)(?=[\s/>=])', re.I)


def strip_calls(text):
    """Remove control envelopes; an unclosed envelope quarantines its tail."""
    private = PrivateMarkupGuard()
    output, stack, cursor = [], [], 0
    for match in _TAG.finditer(text):
        if not stack:
            output.append(text[cursor:match.start()])
        tag = match.group()
        legacy, dialect = _LEGACY.match(tag), private._tag(tag)
        role = ('', legacy[2].lower()) if legacy else dialect[:2] if dialect else None
        closing = bool(legacy[1]) if legacy else dialect[2] if dialect else False
        control = bool(legacy) or bool(dialect and dialect[1] in CALL_ROLES + INVOKE_ROLES + PARAMETER_ROLES)
        if control:
            if closing:
                if stack and role == stack[-1]:
                    stack.pop()
            elif not tag.rstrip().endswith('/>'):
                stack.append(role)
        elif not stack:
            output.append(tag)
        cursor = match.end()
    if not stack:
        output.append(text[cursor:])
    return ''.join(output)


def safe_terminal(text, native_calls):
    guard = OutputGuard()
    visible = guard.feed(text) + guard.tail()
    error = None
    try:
        guard.check()
    except VastarError as exc:
        error = exc
        guard = OutputGuard()
        visible = guard.feed(strip_calls(text)) + guard.tail()
        try:
            guard.check()
        except VastarError:
            # An incomplete reserved tag can remain after envelope stripping.
            # Retain only the prefix already emitted by the unchanged guard.
            guard = OutputGuard()
            visible = guard.feed(visible) + guard.tail()
            guard.check()
    if native_calls and error is None:
        error = VastarError(ErrorCode.UNPARSED_TOOL_CALL, ErrorCategory.PROTOCOL,
            '无工具终答中的调用未执行；正文保留。', retryable=False,
            details={'diagnostic_only': True, 'tool_call_count': native_calls})
    return visible, error


async def terminal_output(frames, rejected, interrupted):
    """Buffer terminal text only; every emitted character passes the existing guard."""
    raw, native_calls, last, end = [], 0, 0, None
    try:
        async with aclosing(frames):
            async for frame in frames:
                if frame.kind == FrameKind.CONTENT:
                    raw.append(frame.text or '')
                    native_calls += len(frame.tool_calls or [])
                    last = frame.source_chunk_index
                elif frame.kind == FrameKind.FINISH:
                    end = frame
                else:
                    yield frame
    except BaseException:
        await interrupted(''.join(raw), native_calls)
        raise
    text, error = safe_terminal(''.join(raw), native_calls)
    if error:
        await rejected(error)
    if not text.strip():
        if error:
            if native_calls:
                error.code = ErrorCode.TOOL_REJECTED
            raise error
        raise VastarError(ErrorCode.NO_FINAL_CONTENT, ErrorCategory.PROTOCOL,
                          '无工具终答未产生正文。', retryable=False)
    yield ProviderFrame(kind=FrameKind.CONTENT, text=text, source_chunk_index=last)
    if end:
        yield replace(end, finish_reason='stop' if end.finish_reason == 'tool_calls' else end.finish_reason)
