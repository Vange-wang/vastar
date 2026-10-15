"""Accepted task, policy-governed procedure and independent process evidence."""
import copy

from core.harness.task_completion import _recorded_results
from core.harness.validators import Validator
from core.skills.release_package import digest, require


def accepted(service, task_id):
    task = service.store.task_get(task_id)
    require(task and task['project_id'] == service.project and task['status'] == 'COMPLETED'
            and task['steps'], 'accepted_source_task_required')
    validator = Validator(service.store, service.context.artifacts)
    covered = set()
    for step in task['steps']:
        require(step['status'] == 'COMPLETED', 'source_step_incomplete')
        recorded = _recorded_results(service.store, task_id, step)
        for key in step['acceptance_ids']:
            result = recorded.get(key, {})
            require(result.get('passed') is True and validator.check(task['acceptance'][key], result.get('evidence', {}))['passed'],
                    'source_acceptance_stale')
            covered.add(key)
    require(covered == set(task['acceptance']), 'source_acceptance_missing')
    return task


def procedure(service, session, key):
    memory = service.context.memory_service
    require(memory is not None, 'memory_dependency_missing')
    found = [i for i in memory.list(session)['items'] if i['key'] == key and i['kind'] == 'procedure'
             and i['scope'] == 'project:' + service.project]
    require(len(found) == 1, 'active_policy_procedure_required')
    return found[0]


def current(service, row):
    item = procedure(service, row['author_session'], row['memory_key'])
    require(item['id'] == row['memory_id'] and item['revision'] == row['memory_revision'], 'experience_changed_or_revoked')
    task = accepted(service, row['source_task'])
    require(digest(task) == row['source_hash'], 'source_task_changed')


def independent(service, row, session, change_id):
    from core.skills import code_change, code_change_state
    require(session != row['author_session'], 'independent_session_required')
    review_task = service.context.task_service.current(session)['task_id']
    require(review_task not in {row['source_task'], row['author_task']}, 'independent_task_required')
    checked = code_change_state.load(service.context, session, change_id)
    require(checked['facts']['owner'][2] != row['source_task'], 'independent_task_required')
    report = code_change.report(service.context, checked)
    require(report['verified'] is True, 'independent_process_checks_failed')
    facts = checked['facts']
    require(code_change.unchanged(checked).get('release.json') == row['hash'], 'validation_bundle_hash_mismatch')
    checks = facts['definition']['checks']
    require(len(checks) == 4 and {c['rationale'] for c in checks} == {'input', 'success', 'failure', 'disabled'},
            'four_independent_checks_required')
    return {'session': session, 'change_id': change_id, 'scope_operation': checked['operation_id'],
            'facts_hash': digest(copy.deepcopy(facts)), 'bundle_hash': row['hash'], 'checks': report['checks']}


def quiescent(service):
    processes = getattr(service.context, 'process_service', None)
    require(processes is not None, 'process_dependency_missing')
    closed = {'completed', 'failed', 'cancelled', 'nonzero_exit', 'signal_terminated', 'timeout', 'idle_timeout', 'output_limit'}
    require(all(e['info']['status'] in closed for e in processes.entries.values()),
            'stop_inflight_processes_before_rollback')
    for event in service.store._conn.execute("SELECT operation_id FROM tool_operations WHERE tool='start_process' AND state NOT IN ('committed','not_committed')"):
        facts = service.store.operation_get(event[0])['facts']
        require(facts.get('process', {}).get('status') in closed, 'reconcile_process_receipt_before_rollback')
    pending = service.store._conn.execute(
        "SELECT 1 FROM tool_operations WHERE classification!='read' AND state IN ('prepared','executing','unknown')"
        " AND tool NOT IN ('start_process','code_change') LIMIT 1").fetchone()
    require(pending is None, 'reconcile_external_operations_before_rollback')
