"""历史入口：列表分页/范围检索/索引重建；不读取全库。"""
import json

from core.history_tools import SEARCH_HISTORY, search
from memory.history_index import rebuild


def history_command(coord, arg):
    service = getattr(getattr(coord, 'task_tools_context', None), 'history_service', None)
    if service is None:
        return {'status': 'unavailable', 'reason': '历史服务未装配'}
    cmd, _, tail = arg.partition(' ')
    if cmd in ('', 'help'):
        return ('/history list [JSON: after,limit,source_ids,since,until]；/history search <JSON: query,...>；'
                '/history rebuild；/history tools enable|disable。只搜本用户本项目历史，分数不是事实概率。')
    if cmd == 'tools' and tail in ('enable', 'disable'):
        enabled = coord.task_tools_context.registry.enabled
        if tail == 'enable' and SEARCH_HISTORY not in enabled:
            enabled.append(SEARCH_HISTORY)
        elif tail == 'disable' and SEARCH_HISTORY in enabled:
            enabled.remove(SEARCH_HISTORY)
        return '历史检索工具已' + ('启用' if tail == 'enable' else '关闭')
    if cmd == 'rebuild':
        return rebuild(service)
    args = json.loads(tail or '{}')
    if cmd == 'search':
        return search(service, args)
    if cmd == 'list':
        return service.list(**args)
    raise ValueError('用 /history help 查看用法')


def handle_history(ui, arg):
    try:
        result = history_command(ui._coordinator, arg)
        ui._renderer.render_note(result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, TypeError) as exc:
        ui._renderer.render_note(f'历史查询不可用：{exc}')
