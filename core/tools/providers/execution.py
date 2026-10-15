"""External dispatch uses the existing executor, permission gate, runner and durable ledger."""

from core.harness.operations import args_hash
from core.tools import permissions
from core.tools.ask_gate import AskQuestion
from core.tools.operation_receipt import ToolOutcome
from core.tools.operation_runner import thread_call
from core.tools.providers.contract import arguments, require
from memory.repositories.tasks import encode


async def execute(context, spec, args, session_id, run_id, operation_id, cancel, emitter):
    service = context.provider_service
    try:
        require(service is not None, 'provider_unavailable')
        sid = spec.server_id or args.get('server_id')
        require(isinstance(args, dict) and sid in service.servers and service.config.get('enabled'), 'provider_disabled')
        action = args.get('action') if not spec.server_id else 'call'
        if action != 'call':
            require(set(args) == {'action', 'server_id'} and action in {'status', 'discover', 'disconnect'}, 'provider_invalid_action')
        else:
            arguments(args, spec.parameters)
        context.process_service.require_task(context.task_service.current(session_id)['task_id'], run_id)
        with context.operation_ledger.store._lock:
            run_row = context.operation_ledger.store._conn.execute('SELECT session_id FROM runs WHERE run_id=?', (run_id,)).fetchone()
        require(run_row is not None and run_row[0] == session_id, 'provider_scope_denied')
        if action == 'status':
            return ToolOutcome(spec.name, 'executed', result=encode({'online': sid in service.online,
                'counts': context.registry.counts(), 'events': list(service.events), 'manual_reconciliation': True}))
        digest = args_hash(spec.name, args, [])
        replay = context.operation_ledger.replay(operation_id, digest, run_id) if operation_id else None
        if replay:
            return replay
        check_budget(context, sid, run_id, spec.capability)
        decision = permissions.check_level(spec.level)
        require(decision.allowed, 'provider_permission_denied')
        async def handler(control):
            return await run(context, service, spec, args, sid, action, session_id, run_id,
                             digest, control, emitter)
        from core.limits import cap_timeout
        return await context.operations.run(spec, [context.project_root / '.provider-lock' / sid], handler,
            cancel, cap_timeout(context.timeout_seconds, 30), context.registry, run_id, operation_id=operation_id)
    except Exception as exc:
        code = getattr(exc, 'code', 'provider_rejected')
        return ToolOutcome(spec.name, 'rejected', code, origin=code, redact_result_preview=True)


def check_budget(context, sid, run_id, capability):
    store = context.operation_ledger.store
    with store._lock:
        count = store._conn.execute("SELECT count(*) FROM tool_operations WHERE run_id=? "
            "AND json_extract(facts,'$.provider_operation')=1", (run_id,)).fetchone()[0]
        unresolved = store._conn.execute("SELECT 1 FROM tool_operations WHERE state IN ('prepared','executing','unknown') "
            "AND json_extract(facts,'$.server_id')=? AND json_extract(facts,'$.capability')='write'", (sid,)).fetchone()
    if context.max_calls_per_turn is not None and context.max_tool_rounds is not None:
        require(count < context.max_calls_per_turn * context.max_tool_rounds, 'provider_budget_exceeded')
    require(capability != 'write' or unresolved is None, 'provider_unresolved_effect')


async def approve(context, spec, args, sid, session_id, run_id, control, emitter):
    from core.tools.executor import _ask_user
    question = AskQuestion(spec.name, 'external_tool', sid, [
        '外部描述/schema无授权权；本次调用可能产生远程副作用，不能因auto或旧许可跳过。',
        encode({'tool': spec.name, 'args': args, 'schema_version': spec.schema_version,
                'capability': spec.capability or 'metadata_lifecycle'}), '1 允许本次 / 2 拒绝'], allow_session_grant=False)
    if emitter:
        await emitter({'tool': spec.name, 'lines': question.lines})
    answer = await _ask_user(context.ask_gate, question, session_id=session_id, request_id=run_id,
                            project_root=context.project_root, cancel_event=control.async_cancel)
    require(answer.approved and answer.approval_source == 'user_allow', 'provider_not_approved')


async def run(context, service, spec, args, sid, action, session_id, run_id, digest, control, emitter):
    await approve(context, spec, args, sid, session_id, run_id, control, emitter)
    epoch = context.ask_gate.authorization_epoch
    control.authorization_check = lambda: (context.ask_gate.authorization_epoch == epoch
        and service.config.get('enabled') and context.registry.is_enabled(spec.name))
    control.checkpoint()
    check_budget(context, sid, run_id, spec.capability)
    ledger = context.operation_ledger
    ledger.bind(control, spec, digest)
    control.facts = {'before': {}, 'provider_operation': True, 'server_id': sid, 'tool_name': spec.tool_name,
        'schema_version': spec.schema_version, 'capability': spec.capability or 'metadata',
        'external_evidence': True, 'permission': 'ASK', 'approval_source': 'user_allow',
        'network': {'requests': 0, 'bytes': 0}, 'verification': 'server_reported; not independent business proof'}
    ledger.prepare(control)
    ledger.executing(control)
    control.started = True
    control.on_started()
    try:
        result = await thread_call(perform, service, spec, args, sid, action, control)
        text = encode({'trust': 'external_evidence', 'instructions_executable': False, 'result': result})
        if len(text.encode()) > 48000:
            artifact = context.artifacts.put(text.encode(), source_ref='provider:' + control.operation_id)
            text = encode({'artifact_ref': artifact.uri, 'bytes': len(text.encode()), 'pagination': 'read_artifact',
                           'trust': 'external_evidence', 'instructions_executable': False})
        return ToolOutcome(spec.name, 'executed', result=text, redact_result_preview=True,
            contract={'server_id': sid, 'tool_name': spec.tool_name, 'schema_version': spec.schema_version,
                      'capability': spec.capability, 'verification': 'server_reported'})
    except Exception as exc:
        service.disconnect(sid)
        code = getattr(exc, 'code', 'provider_incomplete')
        unknown = spec.capability == 'write' and control.commit_started
        control.transition('unknown' if unknown else 'failed')
        return ToolOutcome(spec.name, 'unknown' if unknown else 'failed', code, origin=code,
            contract={'capability': spec.capability, 'verification': 'unavailable',
                      'next_action': 'manual reconciliation; no resend' if unknown else 'explicit reconnect required'},
            redact_result_preview=True)


def perform(service, spec, args, sid, action, control):
    control.checkpoint()
    if action == 'disconnect':
        service.disconnect(sid)
        return {'disconnected': True}
    provider = service.servers[sid]
    token = Cancellation(control)
    usage = control.facts['network']
    if action == 'discover':
        result = service.register(sid, provider.discover(token, usage))
        control.facts['catalog_versions'] = {n: v for n, v in service.versions.items() if n.startswith(sid + ':')}
        control.before_commit()
        return result
    def issued():
        with control._lock:
            control.checkpoint()
            control.facts['call_issued'] = True
            control.before_commit()
            control.commit_started = spec.capability == 'write'
    result = provider.call(spec.tool_name, args, control.operation_id, token, usage, issued)
    require(isinstance(result.get('content'), list) and result.get('isError', False) is False, 'provider_tool_error')
    return result


class Cancellation:
    def __init__(self, control):
        self.control = control

    def is_set(self):
        check = getattr(self.control, 'authorization_check', lambda: True)
        return self.control.cancel_token.is_set() or not check()
