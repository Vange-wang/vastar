"""T23：可自由预览，执行只认当前用户输入中完整的预览确认。"""
import json

from core.tools.operation_receipt import ToolOutcome
from core.tools.registry import ToolSpec
from memory.forgetting import ForgettingService

FORGET_MEMORY = 'forget_memory'


def install_forgetting(context, coordinator):
    service = ForgettingService(context.memory_service, context.artifacts)
    coordinator.forgetting = context.memory_service.forgetting = service
    context.registry.register(ToolSpec(
        name=FORGET_MEMORY, description='T23：先 preview 影响集；apply 必须有当前用户 /memory forget JSON 确认。'
        'withdraw 撤回、rollback 回旧有效版本、hard_delete 隐私删除；回执列未完成清理。',
        parameters={'type': 'object', 'properties': {'action': {'enum': ['preview', 'apply']},
                    'request': {'type': 'object'}, 'preview_id': {'type': 'string'}},
                    'required': ['action', 'request'], 'additionalProperties': False},
        level='A', run=lambda **kwargs: '', effect='delete', resource_scope='memory', retryable=False,
        cancel_phase='before_commit', reconcile='reread'))


def execute_forgetting(context, args, session_id, run_id, operation_id, cancel_event):
    try:
        service = context.memory_service.forgetting
        if set(args) - {'action', 'request', 'preview_id'}:
            raise ValueError('unsupported_forget_fields')
        if args['action'] == 'preview':
            receipt = service.preview(session_id, args['request'])
        elif args['action'] == 'apply':
            row = service.store._conn.execute('SELECT m.content,m.role FROM runs r JOIN messages m '
                                               'ON m.message_id=r.input_message_id WHERE r.run_id=? AND r.session_id=?',
                                               (run_id, session_id)).fetchone()
            prefix = '/memory forget '
            if row is None or row['role'] != 'user' or not row['content'].startswith(prefix):
                raise ValueError('forget_requires_current_user_confirmation')
            confirmed = json.loads(row['content'][len(prefix):])
            if confirmed != {'request': args['request'], 'preview_id': args['preview_id']}:
                raise ValueError('forget_confirmation_mismatch')
            receipt = service.execute(session_id, args['request'], args['preview_id'], user_confirmed=True,
                                      action_id=operation_id, cancel_event=cancel_event)
        else:
            raise ValueError('invalid_forget_action')
        return ToolOutcome(FORGET_MEMORY, 'executed', result=json.dumps(receipt, ensure_ascii=False),
                           origin='memory_policy', audit={'memory_receipt': receipt})
    except (ValueError, KeyError, TypeError) as exc:
        return ToolOutcome(FORGET_MEMORY, 'rejected', reason=str(exc), origin='memory_policy')
