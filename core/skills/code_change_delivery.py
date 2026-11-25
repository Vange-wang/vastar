"""Recheck a frozen S06 command against its scope, native process and committed edits."""
from pathlib import Path

from core.skills import code_change_state as state
from core.skills.code_change import unchanged
from core.skills.code_change_snapshot import require


def _bound_operation(store, operation_id, owner):
    op = store.operation_get(operation_id)
    require(op and (op['task_id'], op['step_id'], op['run_id']) == owner, 'code_delivery_owner_mismatch')
    return op


def _native_check(store, owner, scope_row, hashes, check_index, result):
    facts = scope_row['facts']
    spec = facts['definition']['checks'][check_index]
    attempt = next((a for a in reversed(facts['attempts']) if a['check'] == check_index
                    and a['hashes'] == hashes and 'info' in a), None)
    require(attempt and result['command'] == spec['command'] and result['classification'] == 'passed'
            and result['process'] == attempt['info'] and state.passed(attempt['info'])
            and attempt.get('probe') == state.baseline_probe(facts['baseline']), 'code_delivery_check_unverified')
    job = _bound_operation(store, attempt['info']['operation_id'], owner)
    native = job['facts'].get('process', {})
    manifest = job['facts'].get('document', {})
    require(job['tool'] == 'start_process' and job['facts'].get('approval_source') == 'user_allow'
            and manifest.get('kind') == 'code-validation' and manifest.get('command') == spec['command']
            and manifest.get('workspace') == facts['definition']['workspace'] and manifest.get('hashes') == hashes
            and manifest.get('baseline_probe') == attempt['probe'] and native.get('owner') == owner[0]
            and native.get('job_id') == attempt['info']['job_id'] and state.passed(native),
            'code_delivery_native_receipt_mismatch')
    return job['operation_id']


def _accounted_jobs(store, row, owner):
    """Identify only the declared offline probe and terminal validation jobs for this scope."""
    facts = row['facts']
    baseline = facts['baseline']
    definition = facts['definition']
    jobs = [(baseline['job'], None, facts['hashes'], None)]
    for attempt in facts['attempts']:
        if 'not_started' in attempt and 'job' not in attempt:
            continue
        command = definition['checks'][attempt['check']]['command']
        jobs.append((attempt['job'], command, attempt['hashes'], state.baseline_probe(baseline)))
    accounted = {row['operation_id']}
    for issued, command, hashes, probe in jobs:
        op = _bound_operation(store, issued['operation_id'], owner)
        document = op['facts'].get('document', {})
        native = op['facts'].get('process', {})
        require(op['tool'] == 'start_process' and op['state'] in {'unknown', 'committed'}
                and op['facts'].get('approval_source') == 'user_allow'
                and document.get('kind') == 'code-validation' and document['command'] == command
                and document['workspace'] == definition['workspace'] and document['hashes'] == hashes
                and document['baseline_probe'] == probe and native.get('owner') == owner[0]
                and native.get('job_id') == issued['job_id'] and native.get('operation_id') == op['operation_id']
                and native.get('status') in {'completed', 'nonzero_exit'} and type(native.get('exit_code')) is int
                and (command is not None or state.passed(native)), 'code_delivery_process_unresolved')
        accounted.add(op['operation_id'])
    return accounted


def unaccounted_operations(store, task, pending=None):
    """Preserve pending effects; only previously validated offline job identities may pass."""
    from core.harness.task_completion import recorded_results

    if pending is None:
        pending = [op['operation_id'] for op in store.operations_pending(task['task_id'])]
    remaining = set(pending)
    for step in task['steps']:
        if not remaining or step['status'] != 'COMPLETED':
            continue
        for key, result in recorded_results(store, task['task_id'], step).items():
            criterion = task['acceptance'].get(key)
            if (key not in step['acceptance_ids'] or not isinstance(criterion, dict)
                    or criterion.get('kind') != 'code_check' or not isinstance(result, dict) or result.get('kind') != 'code_check'
                    or result.get('passed') is not True or not result.get('accounted_operation_ids')):
                continue
            owner = (task['task_id'], step['step_id'], result['run_id'])
            operation = _bound_operation(store, result['evidence']['operation_id'], owner)
            require(operation['tool'] == 'code_change', 'code_delivery_invalid_scope')
            accounted = _accounted_jobs(store, operation, owner)
            require(accounted == set(result['accounted_operation_ids']), 'code_delivery_process_unresolved')
            remaining.difference_update(accounted)
    return [oid for oid in pending if oid in remaining]


def _verified_files(store, owner, row, hashes, changes):
    facts = row['facts']
    scope = facts['definition']['scope']
    changed = [p for p in scope if facts['hashes'].get(p) != hashes.get(p)]
    require(changed and set(changes) == set(changed), 'code_delivery_changes_mismatch')
    after = {}
    for edit in facts['edits']:
        if edit['status'] != 'executed':
            continue
        op = _bound_operation(store, edit['audit']['operation_id'], owner)
        require(op['state'] == 'committed' and op['tool'] == edit['tool']
                and op['tool'] in {'edit_file', 'write_file', 'move_file', 'delete_path'}
                and op['facts'].get('after') == edit['audit']['after'], 'code_delivery_edit_unverified')
        for path, value in op['facts']['after'].items():
            after[path] = (value, op['operation_id'])
    outputs = []
    for rel in changed:
        path = str(Path(facts['definition']['workspace']) / rel)
        before, digest = facts['hashes'].get(rel), hashes.get(rel)
        proof, op_id = after.get(path, ({}, None))
        status = 'added' if before is None else 'deleted' if digest is None else 'modified'
        expected = {'exists': False} if digest is None else {'exists': True, 'sha256': digest}
        require(proof == expected and changes[rel]['status'] == status and changes[rel]['before_sha256'] == before
                and changes[rel]['after_sha256'] == digest, 'code_delivery_file_receipt_mismatch')
        outputs.append({'path': path, 'status': status, 'sha256': digest, 'operation_id': op_id})
    return outputs


def verify_code_check(store, artifacts, criterion, evidence, *, task_id, step_id, run_id):
    """Call on step delivery and on final recheck, using the step's actual persisted owner IDs."""
    require(isinstance(evidence, dict) and set(evidence) <= {'operation_id', 'artifact_ref'}
            and isinstance(evidence.get('operation_id'), str), 'code_delivery_scope_required')
    owner = (task_id, step_id, run_id)
    row = _bound_operation(store, evidence['operation_id'], owner)
    # An archived, read-only proof survives reopening the project. state.load()
    # still fences all live edits/verifications against the current generation.
    require(row['tool'] == 'code_change', 'code_delivery_invalid_scope')
    definition = row['facts']['definition']
    require(isinstance(criterion, dict) and isinstance(criterion.get('command'), str), 'code_delivery_command_mismatch')
    scope = criterion.get('scope')
    require(criterion.get('kind') == 'code_check' and isinstance(criterion.get('workspace'), str)
            and Path(criterion['workspace']).is_absolute()
            and Path(criterion['workspace']).resolve() == Path(definition['workspace'])
            and isinstance(scope, list) and len(scope) == len(definition['scope'])
            and all(isinstance(p, str) for p in scope) and set(scope) == set(definition['scope']),
            'code_delivery_scope_mismatch')
    report, _, reference = state.read_review(artifacts, row, evidence.get('artifact_ref'))
    results = report['results']
    require(report['scope'] == definition['scope'] and report['workspace'] == definition['workspace']
            and len(results) == len(definition['checks']), 'code_delivery_report_mismatch')
    hashes = unchanged(row)
    jobs = {check['command']: _native_check(store, owner, row, hashes, i, check) for i, check in enumerate(results)}
    require(criterion.get('command') in jobs, 'code_delivery_command_mismatch')
    accounted = _accounted_jobs(store, row, owner)
    pending = {op['operation_id'] for op in store.operations_pending(task_id) if op['step_id'] == step_id}
    require(pending <= accounted, 'code_delivery_unrelated_pending_operation')
    outputs = _verified_files(store, owner, row, hashes, report['file_changes'])
    if 'changed_paths' in criterion:
        expected = criterion['changed_paths']
        require(isinstance(expected, list) and all(isinstance(p, str) for p in expected)
                and set(expected) == set(report['file_changes']), 'code_delivery_changes_mismatch')
    return {'evidence': {'operation_id': row['operation_id'], 'artifact_ref': reference},
            'outputs': outputs, 'process_operation_id': jobs[criterion['command']],
            'accounted_operation_ids': sorted(accounted)}
