"""S09 binding and read-only retrieval. No model tool can authorize an archive write."""
import json

from core.tools.registry import ToolSpec
from memory.knowledge import ProjectKnowledge

READ_KNOWLEDGE = 'read_project_knowledge'


def install_knowledge(context, coordinator):
    memory = getattr(context, 'memory_service', None)
    knowledge = ProjectKnowledge(memory, context.project_root, context.cwd)
    coordinator.knowledge = knowledge
    skills = getattr(context, 'skills', None)
    if skills is not None:
        skills.bind_memory(memory)
    context.registry.register(ToolSpec(
        name=READ_KNOWLEDGE, description='只读当前项目归档；find返回有效引用，open打开当前原证据；缺失依赖明确报错，不授权写入。',
        parameters={'type': 'object', 'properties': {'action': {'type': 'string', 'enum': ['find', 'open']},
                    'query': {'type': 'string'}, 'reference': {'type': 'string'}},
                    'required': ['action'], 'additionalProperties': False}, level='B',
        run=lambda args, **kwargs: read_knowledge(knowledge, coordinator.session_id, args),
        effect='read', resource_scope='memory', reconcile='reread'))


def read_knowledge(knowledge, session_id, args):
    if set(args) - {'action', 'query', 'reference'}:
        raise ValueError('knowledge_read_only_arguments')
    if args.get('action') == 'find':
        result = knowledge.find(session_id, query=args.get('query', ''))
    elif args.get('action') == 'open':
        result = knowledge.open(session_id, args['reference'])
    else:
        raise ValueError('knowledge_read_only_actions')
    return json.dumps(result, ensure_ascii=False)
