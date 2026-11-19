"""Async execution command; legacy team inspection and management remain synchronous."""
import json


async def handle_team_entry(ui, arg):
    from ui.team_commands import handle_team
    if not arg.startswith('run '):
        handle_team(ui, arg)
        if arg in ('', 'help'):
            ui._renderer.render_note('/team run {"children":[{"goal":"目标","read_paths":["路径"],'
                '"required_facts":["须保留的原文事实"],"write_path":"可选，必须已纳入 Task 文件验收"}]}；'
                '显式开启编排后可用，预算与并行度由宿主决定；Ctrl+C 取消。')
        return
    from core.artifacts import ArtifactError
    from core.orchestration.entry import run_team
    try:
        result = await run_team(ui._coordinator, arg[4:], ui._renderer.render_note)
        ui._renderer.render_note(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, KeyError, TypeError, ArtifactError) as exc:
        ui._renderer.render_note(f'Team：{exc}；宿主任务未完成')


def cancel_team(coord):
    event = getattr(coord, '_team_cancel', None)
    if event is None:
        return False
    event.set()
    return True
