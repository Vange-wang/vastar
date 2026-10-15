"""Durable action boundary. A dispatched command is not a committed business effect."""
from dataclasses import asdict

from core.harness.operations import args_hash
from core.tools.browser_protocol import BrowserFailure
from core.tools.operation_control import OperationControl
from core.tools.operation_receipt import ToolOutcome
from memory.repositories.tasks import encode


def prepare(context, spec, args, owner, run_id, operation_id, resources, entry):
    control = OperationControl(run_id)
    control.operation_id = operation_id
    context.operation_ledger.bind(control, spec, args_hash(spec.name, args, []))
    control.facts = {'before': {'browser': {k: args[k] for k in ('tab_id', 'page_revision', 'origin')}},
        'owner': owner, 'resource_keys': resources, 'permission': 'ALLOW', 'approval_source': 'user_allow',
        'external_evidence': True, 'browser_action': {**args, 'target': entry}, 'action_issued': False,
        'rollback': 'issued click/submission cannot be undone by adapter rollback'}
    context.operation_ledger.prepare(control)
    context.operation_ledger.executing(control)
    return control


def permit(control, authorized):
    if not authorized():
        raise BrowserFailure('authorization_revoked')
    control.facts['action_issued'] = True
    control.ledger.begin_commit(control)
    control.commit_started = True
    control.transition('executing')


def finish(context, spec, control, payload=None, reason='', *, cancelled=False):
    verified = control.facts.get('business_proof', {}).get('verified') is True
    state = 'committed' if verified else 'unknown' if control.commit_started else 'failed'
    control.facts['cancel_requested'] = cancelled
    control.facts['cancel_phase'] = 'after_dispatch' if cancelled and control.commit_started else 'before_dispatch' if cancelled else None
    control.transition(state)
    outcome = ToolOutcome(spec.name, 'executed' if verified else state, reason=reason,
        result=encode({'operation_id': control.operation_id, 'state': state, 'repeated': False,
                       'proof': control.facts.get('business_proof'), 'snapshot': payload}),
        origin='browser_action', audit={'operation_id': control.operation_id, 'operation_state': state,
            'resource_keys': control.facts['resource_keys'], 'cancel_requested': cancelled,
            'cancel_phase': control.facts['cancel_phase']}, redact_result_preview=True)
    context.operation_ledger.store.operation_finish(control.operation_id, state, asdict(outcome), control.facts)
    return outcome
