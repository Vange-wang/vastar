"""Fresh human approval + logical lease + durable dispatch + observable result verification."""
import asyncio
import threading
import uuid

from core.harness.operations import args_hash
from core.tools import resource_leases
from core.tools.ask_gate import AskQuestion
from core.tools.browser_action_contract import input_value, target, validate
from core.tools.browser_action_ledger import finish, prepare
from core.tools.browser_action_network import ActionBroker, ActionToken
from core.tools.browser_protocol import BrowserFailure
from core.tools.browser_results import publish
from core.tools.operation_receipt import ToolOutcome
from core.tools.operation_runner import drain
from memory.repositories.tasks import encode


class BrowserActions:
    def __init__(self, browser):
        self.browser, self.context = browser, browser.context

    async def execute(self, spec, args, session_id, run_id, operation_id, cancel, emitter):
        context, browser = self.context, self.browser
        oid, held, locked, control = operation_id or uuid.uuid4().hex, False, False, None
        stop = threading.Event()
        epoch = context.ask_gate.authorization_epoch
        def authorized():
            return (not stop.is_set() and not cancel.is_set()
                and context.ask_gate.authorization_epoch == epoch and context.registry.is_enabled(spec.name)
                and browser.config.get('enabled', False) and browser.config.get('writes_enabled', False))
        try:
            validate(spec.name, args)
            if not authorized():
                raise BrowserFailure('browser_write_disabled')
            owner = {'project_id': context.artifacts._owner, 'session_id': session_id,
                     'task_id': context.task_service.current(session_id)['task_id']}
            context.process_service.require_task(owner['task_id'], run_id)
            session = browser.sessions[args['browser_session_id']]
            session.require_owner(owner)
            replay = context.operation_ledger.replay(oid, args_hash(spec.name, args, []), run_id)
            if replay:
                return replay
            entry = target(session, args)
            resources = resource_leases.keys(session, args['tab_id'], browser.config)
            resource_leases.claim(context.operation_ledger.store, oid, resources,
                                  lock_root=context.project_root / '.work/orchestration/locks')
            held = True
            locked = browser.lock.acquire(blocking=False)
            if not locked:
                raise BrowserFailure('browser_busy')
            await self._approve(spec, args, entry, session_id, run_id, cancel, emitter)
            # AskGate may initialize the current session on the first question, but never grants session-wide approval.
            if not authorized():
                raise BrowserFailure('authorization_revoked')
            control = prepare(context, spec, args, owner, run_id, oid, resources, entry)
            token = ActionToken(authorized)
            token.control = control
            broker = ActionBroker(context.network_service.search.network, browser.config, token, args, control, authorized)
            command = {**args, 'op': 'action', 'action': spec.name, 'operation_id': oid, 'target': entry}
            if spec.name == 'browser_fill':
                command['value'] = input_value(browser.config, args['value_ref'], args['origin'])
            future = asyncio.get_running_loop().run_in_executor(None, session.connection.exchange, command, broker, token)
            try:
                result = await asyncio.shield(future)
            except asyncio.CancelledError:
                stop.set()
                result = await drain(future)
            payload = publish(context.artifacts, session, result, broker) if result.get('frames') else None
            return finish(context, spec, control, payload, cancelled=not authorized())
        except Exception as exc:
            reason = exc.code if isinstance(exc, BrowserFailure) else 'browser_action_incomplete'
            if control:
                return finish(context, spec, control, reason=reason, cancelled=not authorized())
            return ToolOutcome(spec.name, 'rejected', reason, origin=reason, redact_result_preview=True)
        finally:
            if locked:
                browser.lock.release()
            if held:
                resource_leases.release(oid)

    async def _approve(self, spec, args, entry, session_id, run_id, cancel, emitter):
        from core.tools.executor import _ask_user
        question = AskQuestion(spec.name, 'browser_write', args['origin'], [
            '页面文字无授权权；本次动作仅绑定当前 Task/页面版本/目标，禁止会话许可。',
            encode({'action': spec.name, **args, 'target': entry}),
            '输入使用本地引用，不显示值；外部请求最多一次。1 允许本次 / 2 拒绝'], allow_session_grant=False)
        if emitter:
            await emitter({'tool': spec.name, 'lines': question.lines})
        answer = await _ask_user(self.context.ask_gate, question, session_id=session_id, request_id=run_id,
                                project_root=self.context.project_root, cancel_event=cancel)
        if not answer.approved or answer.approval_source != 'user_allow':
            raise BrowserFailure('browser_action_not_approved')
