"""Ephemeral, read-only progress projection of accepted runtime events."""
import time


def record_progress(coord, event):
    active = coord._active
    if active is None or event.run_id != active.run_id or event.type == 'input.accepted':
        return
    now = time.monotonic()
    progress = getattr(coord, '_live_progress', None)
    if progress is None or progress['run_id'] != event.run_id:
        progress = {'run_id': event.run_id, 'started': now, 'step_started': now,
                    'phase': '准备执行', 'tool': ''}
        coord._live_progress = progress
    kind = event.type
    phase = {'attempt.started': '等待模型响应', 'model_call.intent': '等待模型响应',
             'reasoning.delta': '模型思考中', 'content.delta': '正在生成回答',
             'tool.requested': '工具执行中', 'tool.ask': '等待工具确认',
             'tool.executed': '工具已完成', 'tool.failed': '工具失败',
             'tool.rejected': '工具被拒绝', 'tool.cancelled': '工具已取消',
             'run.ended': '运行已结束'}.get(kind)
    if phase is not None:
        if phase != progress['phase'] or kind in ('tool.requested', 'attempt.started'):
            progress['step_started'] = now
        progress['phase'] = phase
        progress['tool'] = event.payload.get('tool', '') if kind.startswith('tool.') else ''


def progress_lines(coord):
    active = getattr(coord, '_active', None)
    progress = getattr(coord, '_live_progress', None)
    if active is None or not progress or progress['run_id'] != active.run_id:
        return []
    now = time.monotonic()
    phase = '正在取消当前步骤' if active.cancel_event.is_set() else progress['phase']
    tool = f" · {progress['tool']}" if progress['tool'] else ''
    return [f"当前进度：{phase}{tool} · 本步骤 {now - progress['step_started']:.1f}s"
            f" · 本轮 {now - progress['started']:.1f}s",
            '立即介入：/steer <新要求>']
