"""Session catalogue numbers bind to the last displayed snapshot."""
import asyncio
from datetime import datetime

from ui.theme import BRIGHT_BLUE, BRIGHT_YELLOW, box_rules
from ui.welcome import DEFAULT_USER_NAME


async def handle_sessions(ui, cmd, arg):
    try:
        if cmd == '/sessions':
            count = int(arg) if arg else 10
            rows = ui._coordinator.list_sessions(count)
            ui._session_choices = tuple(row['session_id'] for row in rows)
            if not rows:
                ui._renderer.render_note('暂无历史会话')
                return
            lines = ['序号 │ 时间 │ 消息数 │ 首条用户话 │ 当前']
            for i, row in enumerate(rows, 1):
                stamp = datetime.fromisoformat(row['created_at']).astimezone().strftime('%m-%d %H:%M')
                summary = ' '.join((row['first_user'] or '').split())
                summary = summary[:29] + '…' if len(summary) > 30 else summary
                mark = '← 当前' if row['session_id'] == ui._coordinator.session_id else ''
                lines.append(f"{i} │ {stamp} │ {row['message_count']} │ {summary} │ {mark}")
            ui._renderer.render_note('\n'.join(lines))
            return
        choices = getattr(ui, '_session_choices', ())
        number, page = parse_selection(arg)
        if number < 1 or number > len(choices):
            raise ValueError('序号越界，请先 /sessions 查看列表')
        messages = await ui._coordinator.resume_session(choices[number - 1], page=page)
        await render_replay(ui, messages, number, page)
    except ValueError as exc:
        ui._renderer.render_note(f'会话：{exc}' if str(exc) else '会话：请输入有效序号')


def replay_message(renderer, message, user_name=DEFAULT_USER_NAME):
    role, body = message['role'], message['content']
    if role == 'user':
        renderer.render_user_message(user_name, body)
        return
    renderer._close_open_channel()
    top, bottom = box_rules('Vastar' if role == 'assistant' else role, BRIGHT_YELLOW if role == 'assistant' else BRIGHT_BLUE)
    renderer._emit('style.raw', top + '\n')
    renderer._emit('style.content', body + ('' if body.endswith('\n') else '\n'))
    renderer._emit('style.raw', bottom + '\n', flush=True)


def parse_selection(arg):
    parts = arg.split()
    if not parts or not parts[0].isdecimal():
        raise ValueError('请输入有效序号，例如 /resume 2')
    if len(parts) == 1:
        return int(parts[0]), None
    if len(parts) != 3 or parts[1] != '--page' or not parts[2].isdecimal():
        raise ValueError('用法：/resume <序号> [--page <页码>]')
    return int(parts[0]), int(parts[2])


def replay_window(renderer, messages, number, page):
    total = len(messages)
    if page is None:
        renderer.render_note(f'已切换会话 {number}，共 {total} 条消息，以下从第一条完整回放')
        return messages, False
    page = page or 1
    start = (page - 1) * 20
    pages = max(1, (total + 19) // 20)
    renderer.render_note(f'已切换会话 {number}，共 {total} 条；第 {page}/{pages} 页，'
                         f'本页第 {min(start + 1, total)}–{min(start + 20, total)} 条。'
                         f'返回首段：/resume {number} --page 1；'
                         + (f'继续：/resume {number} --page {page + 1}' if page < pages else '已到最后一页'))
    return messages[start:start + 20], True


def cancel_replay(ui):
    if getattr(ui, '_replaying', False):
        ui._replay_cancelled = True
        return True
    return False


async def render_replay(ui, messages, number, page):
    ui._replaying, ui._replay_cancelled = True, False
    count = 0
    try:
        shown, paged = replay_window(ui._renderer, messages, number, page)
        for message in shown:
            if ui._replay_cancelled:
                ui._renderer.render_note(f'回放已中断：已显示 {count}/{len(shown)} 条；'
                                         '当前会话保留，可继续对话或重新 /resume')
                return
            replay_message(ui._renderer, message, getattr(ui, '_user_name', None) or DEFAULT_USER_NAME)
            count += 1
            if count % 20 == 0:
                await asyncio.sleep(0)
        if not paged:
            ui._renderer.render_note(f'完整回放结束：{count} / {len(messages)} 条；可继续对话')
    finally:
        ui._replaying, ui._replay_cancelled = False, False
