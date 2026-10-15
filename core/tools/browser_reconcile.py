"""Observe business evidence only. Reconciliation never replays a click/fill/request."""
import asyncio
from dataclasses import asdict

from core.tools import resource_leases
from core.tools.browser_network import BrowserBroker
from core.tools.browser_protocol import BrowserFailure
from core.tools.operation_receipt import ToolOutcome
from core.tools.operation_runner import drain
from memory.repositories.tasks import encode


async def reconcile(context, args, session_id, run_id, cancel):
    browser, ledger = context.browser_service, context.operation_ledger
    held, locked, lease = False, False, 'reconcile:' + str(args.get('operation_id'))
    try:
        if set(args) != {'operation_id', 'browser_session_id', 'tab_id'} or not all(isinstance(v, str) for v in args.values()):
            raise BrowserFailure('invalid_reconcile')
        if not browser.config.get('enabled', False):
            raise BrowserFailure('browser_disabled')
        row = ledger.store.operation_get(args['operation_id'])
        if row is None or row['tool'] not in {'browser_click', 'browser_fill'}:
            raise BrowserFailure('operation_not_found')
        task = context.task_service.current(session_id)
        owner = {'project_id': context.artifacts._owner, 'session_id': session_id, 'task_id': task['task_id']}
        if row['facts']['owner'] != owner:
            raise BrowserFailure('browser_scope_denied')
        if row['state'] in {'committed', 'failed', 'not_committed'}:
            return ToolOutcome('browser_reconcile', 'executed', result=encode({'state': row['state'],
                'operation_id': row['operation_id'], 'proof': row['facts'].get('business_proof'), 'historical': True}))
        session = browser.sessions[args['browser_session_id']]
        session.require_owner(owner)
        session.tab(args['tab_id'])
        resources = row['facts']['resource_keys'] + resource_leases.keys(session, args['tab_id'], browser.config)
        resource_leases.claim(ledger.store, lease, resources, reconciling=row['operation_id'],
                              lock_root=context.project_root / '.work/orchestration/locks')
        held = True
        locked = browser.lock.acquire(blocking=False)
        if not locked:
            raise BrowserFailure('browser_busy')
        command = {**row['facts']['browser_action'], 'op': 'verify_action', 'action': row['tool'],
                   'operation_id': row['operation_id'], 'tab_id': args['tab_id']}
        broker = BrowserBroker(context.network_service.search.network, browser.config.get('readonly_urls', []),
                               cancel, {})
        future = asyncio.get_running_loop().run_in_executor(None, session.connection.exchange, command, broker, cancel)
        try:
            result = await asyncio.shield(future)
        except asyncio.CancelledError:
            result = await drain(future)
        state = 'committed' if result.get('proof', {}).get('verified') is True else 'unknown'
        facts = row['facts']
        if state == 'committed':
            facts['business_proof'] = {'verified': True, 'kind': result['proof']['kind'], 'reconciled': True}
        outcome = ToolOutcome('browser_reconcile', 'executed', result=encode({'operation_id': row['operation_id'],
            'state': state, 'repeated': False, 'proof': facts.get('business_proof')}), redact_result_preview=True)
        receipt = asdict(ToolOutcome(row['tool'], 'executed', result=outcome.result, redact_result_preview=True)) if state == 'committed' else row['receipt']
        ledger.store.operation_finish(row['operation_id'], state, receipt, facts,
                                      reconciliation={'status': state, 'read_only': True})
        return outcome
    except Exception as exc:
        reason = exc.code if isinstance(exc, BrowserFailure) else 'reconciliation_unavailable'
        return ToolOutcome('browser_reconcile', 'unknown', reason, origin=reason,
                           result=encode({'state': 'unknown', 'repeated': False}))
    finally:
        if locked:
            browser.lock.release()
        if held:
            resource_leases.release(lease)
