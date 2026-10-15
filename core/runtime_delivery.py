"""A30: deliver existing answer/evidence and honest unfinished work at termination."""
from core.errors import VastarError
from core.events import EventType
from core.llm.base import FrameKind
from core.llm.output_guard import OutputGuard


def evidence(s):
    body = ''.join(f.get('text', '') for frames in s.frames_by_attempt.values()
                   for f in frames if f.get('kind') == FrameKind.CONTENT and not f.get('tool_calls')
                   and not f.get('terminal_buffered'))
    if body.strip():
        return body, ''
    messages = s.terminal_tool_receipts
    safe = []
    for message in messages:
        if message.startswith(('[工具未执行]', '[参数解析失败]')):
            continue
        guard = OutputGuard()
        text = guard.feed(message) + guard.tail()
        if guard.blocked:
            safe.append('[回单含控制标记，未用于答案；请查诊断记录。]')
        elif text.strip():
            safe.append(text)
    return '', '\n\n'.join(safe)


async def deliver_partial(s, reason):
    if s.terminal_delivery_done:
        return
    s.terminal_delivery_done = True
    from memory.forget_runtime import check_request
    try:
        check_request(s.request)
    except VastarError:
        return  # A withdrawn generation must not be revived as terminal evidence.
    body, receipts = evidence(s)
    pending = s.pending_terminal_text
    if not body.strip() and not pending.strip() and not receipts:
        return
    s.result.partial = True
    reason_label = {'run_total_timeout': 'Run 总时长超限', 'output_limit': '达到单次输出上限',
                    'stream_closed_without_finish': '流结束但缺少完成信号',
                    'cancelled_by_user': '用户取消', 'input_budget_exceeded': '输入预算不足'}.get(reason, reason)
    s.result.terminal_note = f'未完成：本轮因 {reason_label} 停止，完整回答及尚未核实部分未完成；以上仅为已产出内容。'
    if body.strip():
        s.warnings.append(s.result.terminal_note)
        if pending.strip():
            await s.emit(s.factory.build(EventType.CONTENT_DELTA, {'text': pending, 'source': 'terminal_delivery'}))
        return  # Existing prose is reused verbatim by /continue and child reports.
    text = pending
    if not body.strip() and not pending.strip():
        text += '\n\n已取得工具回单（原文，不代表任务已完成）：\n' + receipts
    text += '\n\n' + s.result.terminal_note
    await s.emit(s.factory.build(EventType.CONTENT_DELTA, {'text': text, 'source': 'terminal_delivery'}))
