"""Human-only archive preview/confirmation commands, separate from model tool arguments."""
import json

from core.knowledge_tools import READ_KNOWLEDGE


def knowledge_command(coordinator, arg):
    knowledge = getattr(coordinator, 'knowledge', None)
    if knowledge is None:
        raise ValueError('dependency_missing：依赖缺失（MemoryService），未归档')
    knowledge.require_memory()
    cmd, _, tail = arg.partition(' ')
    if cmd in ('', 'help'):
        return ('/memory archive preview <JSON path或task_id/key>；/memory archive commit <preview_id>；'
                '/memory archive find <问题关键词>；/memory archive open <knowledge引用>；'
                '/memory archive tools enable|disable；确认仅授权归档，不授权工程操作。')
    if cmd == 'tools' and tail in ('enable', 'disable'):
        enabled = coordinator.task_tools_context.registry.enabled
        if tail == 'enable' and READ_KNOWLEDGE not in enabled:
            enabled.append(READ_KNOWLEDGE)
        elif tail == 'disable' and READ_KNOWLEDGE in enabled:
            enabled.remove(READ_KNOWLEDGE)
        return '项目归档只读检索工具已' + ('启用' if tail == 'enable' else '关闭')
    if cmd in ('preview', 'commit'):
        skills = getattr(coordinator, '_skills', None)
        if skills is None or 'project-knowledge' not in skills.available():
            raise ValueError('project-knowledge技能未启用；基础对话与已有记忆照常')
        if cmd == 'preview':
            return knowledge.preview(coordinator.session_id, json.loads(tail))
        return knowledge.commit(coordinator.session_id, tail.strip(), user_confirmed=True, origin=coordinator._origin)
    if cmd == 'find':
        return knowledge.find(coordinator.session_id, query=tail)
    if cmd == 'open':
        return knowledge.open(coordinator.session_id, tail.strip())
    if cmd == 'history':
        return knowledge.history(coordinator.session_id, tail.strip())
    raise ValueError('/memory archive help 查看归档用法')
