"""T22 单一入口：模型只提供候选，授权来自当前用户原文或范围授权。"""
import json

from core.tools.operation_receipt import ToolOutcome
from core.tools.registry import ToolSpec
from memory.service import MemoryService

WRITE_MEMORY = 'write_memory'


def install_memory(context, coordinator):
    history = getattr(context, 'history_service', None)
    if history is None:
        return
    service = MemoryService(history.store, history.access.user_id, history.access.project_id,
                            agent_id=getattr(coordinator._agent_def, 'name', None))
    context.memory_service = coordinator.memory_service = service
    from core.knowledge_tools import install_knowledge
    install_knowledge(context, coordinator)
    from core.memory_forget_tools import install_forgetting
    install_forgetting(context, coordinator)
    from core.skills.maintenance import install as install_maintenance
    install_maintenance(context, coordinator)
    context.registry.register(ToolSpec(
        name=WRITE_MEMORY, description='T22：提议长期记忆；必须引用原文；用户事实不能来自模型自说。'
        'expected_revision 新建为0、更新为当前版本；矛盾替换须当前用户明确确认。',
        parameters={'type': 'object', 'properties': {
            'key': {'type': 'string'}, 'text': {'type': 'string'},
            'kind': {'type': 'string', 'enum': ['fact', 'preference', 'hypothesis', 'procedure']},
            'source_refs': {'type': 'array', 'items': {'type': 'string'}}, 'scope': {'type': 'string'},
            'validity': {'type': 'object'}, 'expected_revision': {'type': 'integer'}, 'replace': {'type': 'boolean'}},
            'required': ['key', 'text', 'kind', 'source_refs', 'expected_revision'], 'additionalProperties': False},
        level='A', run=lambda **kwargs: '', effect='write', resource_scope='memory', retryable=False,
        cancel_phase='before_commit', reconcile='reread'))


def execute_memory_tool(context, args, session_id, request_id, operation_id, cancel_event):
    try:
        service = context.memory_service
        row = service.store._conn.execute('SELECT input_message_id FROM runs WHERE run_id=? AND session_id=?',
                                          (request_id, session_id)).fetchone()
        if row is None:
            raise ValueError('memory_requires_current_run')
        receipt = service.submit(session_id, args, input_message_id=row[0],
                                  proposal_id=operation_id, cancel_event=cancel_event)
        committed = receipt['status'] in ('committed', 'duplicate')
        return ToolOutcome(WRITE_MEMORY, 'executed' if committed else 'rejected',
                           result=json.dumps(receipt, ensure_ascii=False),
                           reason='' if committed else receipt.get('reason', 'candidate_only'),
                           origin='memory_policy', audit={'memory_receipt': receipt, 'committed': committed})
    except (ValueError, KeyError, TypeError) as exc:
        return ToolOutcome(WRITE_MEMORY, 'rejected', reason=str(exc), origin='memory_policy')
