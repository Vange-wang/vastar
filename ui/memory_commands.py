"""显式记忆命令与 T22 共用 MemoryService；不另写审计旁库。"""
import json

from core.memory_tools import WRITE_MEMORY
from memory.policy import sensitive


def remember(coordinator, arg):
    service = coordinator.memory_service
    coordinator.ensure_session()
    if arg.startswith('{'):
        proposal = json.loads(arg)
    else:
        key, sep, value = arg.partition('=')
        if not sep:
            raise ValueError('/remember key=原文；更新用 JSON 含 expected_revision/replace')
        proposal = {'key': key.strip(), 'text': value.strip(), 'kind': 'fact', 'expected_revision': 0}
    if sensitive(json.dumps(proposal, ensure_ascii=False)):
        return service.submit(coordinator.session_id, proposal)
    mid, _ = coordinator._persist_user_message('/remember ' + arg, coordinator._origin)
    with service.store._lock:
        rev = service.store._conn.execute('SELECT revision FROM history_sources WHERE source_id=?', (mid,)).fetchone()[0]
    proposal = {**proposal, 'source_refs': [f'history:{coordinator.session_id}/{mid}@r{rev}']}
    return service.submit(coordinator.session_id, proposal, input_message_id=mid)


def memory_command(coordinator, arg):
    coordinator.ensure_session()
    service = getattr(coordinator, 'memory_service', None)
    cmd, _, tail = arg.partition(' ')
    if cmd == 'archive':
        from ui.knowledge_commands import knowledge_command
        return knowledge_command(coordinator, tail)
    if service is None:
        raise ValueError('dependency_missing：依赖缺失（MemoryService）')
    if cmd in ('', 'help'):
        return ('/memory archive help；/remember key=原文；/memory list；/memory read on|off；/memory tools enable|disable；'
                '/memory learn <JSON scope/kind/key>|off；/memory preview <JSON>；'
                '/memory forget <JSON request/preview_id>；/memory revalidate；/memory cleanup；/memory forget-tools enable|disable')
    if cmd == 'list':
        return service.list(coordinator.session_id)
    if cmd == 'cleanup':
        return coordinator.forgetting.cleanup()
    if cmd in ('preview', 'forget', 'revalidate'):
        return forgetting_command(coordinator, cmd, tail)
    if cmd == 'forget-tools' and tail in ('enable', 'disable'):
        from core.memory_forget_tools import FORGET_MEMORY
        enabled = coordinator.task_tools_context.registry.enabled
        if tail == 'enable' and FORGET_MEMORY not in enabled:
            enabled.append(FORGET_MEMORY)
        elif tail == 'disable' and FORGET_MEMORY in enabled:
            enabled.remove(FORGET_MEMORY)
        return 'T23 已' + ('启用；执行仍须当前用户确认' if tail == 'enable' else '关闭')
    if cmd == 'read' and tail in ('on', 'off'):
        with service.store.transaction():
            service.store._conn.execute('UPDATE sessions SET memory_read_enabled=? WHERE session_id=?',
                                         (tail == 'on', coordinator.session_id))
        return '记忆读取已' + ('启用（当前会话版本仍冻结）' if tail == 'on' else '关闭')
    if cmd == 'tools' and tail in ('enable', 'disable'):
        enabled = coordinator.task_tools_context.registry.enabled
        if tail == 'enable' and WRITE_MEMORY not in enabled:
            enabled.append(WRITE_MEMORY)
        elif tail == 'disable' and WRITE_MEMORY in enabled:
            enabled.remove(WRITE_MEMORY)
        return 'T22 已' + ('启用' if tail == 'enable' else '关闭')
    if cmd == 'learn':
        if tail == 'off':
            with service.store.transaction():
                service.store._conn.execute("UPDATE sessions SET memory_grants_json='[]' WHERE session_id=?", (coordinator.session_id,))
        else:
            service.grant(coordinator.session_id, json.loads(tail))
        return '候选贡献授权已更新；不全量吸收对话、不跨会话继承'
    raise ValueError('/memory help 查看用法')


def handle_memory(ui, cmd, arg):
    try:
        result = remember(ui._coordinator, arg) if cmd == '/remember' else memory_command(ui._coordinator, arg)
        ui._renderer.render_note(result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        ui._renderer.render_note('记忆不可用：' + str(exc))


def forgetting_command(coordinator, cmd, tail):
    service = coordinator.forgetting
    if cmd == 'revalidate':
        return service.revalidation_queue(coordinator.session_id)
    data = json.loads(tail)
    if cmd == 'preview':
        return service.preview(coordinator.session_id, data)
    # 命令通道的确认只来自真人；不把待删除正文复制到新审计。
    return service.execute(coordinator.session_id, data['request'], data['preview_id'], user_confirmed=True)
