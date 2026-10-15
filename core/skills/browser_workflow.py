"""S07 only narrows permissions; durable attempts never turn into implicit retries."""
import asyncio
import threading

from core.skills import browser_workflow_state as state
from core.skills import browser_workflow_steps as steps
from core.skills.browser_workflow_contract import remedy, require, validate
from core.tools.operation_receipt import ToolOutcome
from core.tools.registry import ToolSpec

NAME, SKILL = 'browser_workflow', 'browser-workflow'
_LOCK, _ACTIVE = threading.Lock(), set()


def active(context):
    return context.skills is not None and SKILL in context.skills.available() and SKILL in context.skills.active_names()


def guard(context, spec):
    delegated = steps.DELEGATE.get() == (id(context), spec.name)
    if active(context) and spec.effect != 'read' and not delegated and spec.name not in {
            NAME, 'ask_task_question', 'terminate_process', 'validate_task_step'}:
        return ToolOutcome(spec.name, 'rejected', '工作流写入必须由已保存步骤推进并逐次确认', origin='workflow_fresh_confirmation')
    return None


def install(context):
    context.registry.register(ToolSpec(NAME,
        '保存、查看或推进一个浏览器流程步骤；默认关闭；逐次人类确认；失败不重试。',
        {'type': 'object', 'properties': {'action': {'enum': ['save', 'advance', 'status']},
         'workflow_id': {'type': 'string'}, 'definition': {'type': 'object'},
         'browser_session_id': {'type': 'string'}, 'tab_id': {'type': 'string'}},
         'required': ['action', 'workflow_id'], 'additionalProperties': False},
        'C', lambda **_: 'special handler required', effect='write', resource_scope='browser', retryable=False))


async def execute(context, args, session_id, run_id, cancel, emitter):
    key, held = None, False
    try:
        require(active(context), 'workflow_skill_disabled')
        validate(args)
        who = state.owner(context, session_id)
        context.process_service.require_task(who['task_id'], run_id)
        key = (str(context.operation_ledger.store._path), state.identity(who, args['workflow_id']))
        with _LOCK:
            require(key not in _ACTIVE, 'workflow_busy')
            _ACTIVE.add(key)
            held = True
        if args['action'] == 'save':
            steps.validate_urls(context, args['definition'])
            row = state.save(context, who, args['workflow_id'], args['definition'], run_id)
        else:
            row = state.refresh(context, state.load(context, who, args['workflow_id']))
        if args['action'] == 'advance':
            require(row['fencing_generation'] == context.operation_ledger.store.fencing_generation,
                    'workflow_restart_requires_review')
            await advance(context, row, args, session_id, run_id, WorkflowCancel(context, cancel), emitter)
        return ToolOutcome(NAME, 'executed', result=state.output(context, row), redact_result_preview=True)
    except Exception as exc:
        code = getattr(exc, 'code', 'workflow_incomplete')
        return ToolOutcome(NAME, 'rejected', code, origin=code, redact_result_preview=True)
    finally:
        if held:
            with _LOCK:
                _ACTIVE.discard(key)


class WorkflowCancel:
    def __init__(self, context, cancel):
        self.context, self.cancel = context, cancel

    def is_set(self):
        return self.cancel.is_set() or not active(self.context) or not self.context.registry.is_enabled(NAME)

    async def wait(self):
        while not self.is_set():
            await asyncio.sleep(.05)


async def advance(context, row, args, session_id, run_id, cancel, emitter):
    facts = row['facts']
    pending = [(i, s) for i, s in enumerate(facts['steps']) if s['state'] != 'committed']
    if not pending:
        state.persist(context, row)
        return
    index, step = pending[0]
    require(not step['attempts'], 'workflow_no_repeat')
    require(not cancel.is_set(), 'cancelled')
    question = steps.clarify(context, facts['definition'], index, session_id)
    if question:
        step['question_id'] = question
        state.persist(context, row)
        return
    step.update(attempts=1, state='unknown')
    state.persist(context, row)  # Durable before any observation, approval or dispatch.
    tab = {k: args[k] for k in ('browser_session_id', 'tab_id')}
    try:
        if facts['definition']['mode'] == 'collect':
            step['record'] = await steps.collect(context, facts['definition'], index, tab,
                                                 session_id, run_id, cancel, emitter, step['operation_id'])
            step['state'] = 'committed'
        else:
            out = await steps.submit(context, facts['definition'], index, tab, session_id, run_id,
                                     cancel, emitter, step['operation_id'])
            step.update(state='failed', error=out.origin or out.status)
            state.refresh(context, row)
    except asyncio.CancelledError:
        step['error'] = 'cancelled'
        raise
    except Exception as exc:
        step.update(state='failed', error=getattr(exc, 'code', 'workflow_observation_failed'))
    finally:
        state.refresh(context, row)
        if step['state'] != 'committed':
            step['remediation'] = remedy(step.get('error', 'unknown'))
        state.persist(context, row)
