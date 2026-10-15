"""Use existing OperationLedger for durable definitions and consumed step attempts."""
import hashlib

from core.harness.operations import args_hash
from core.skills.browser_workflow_contract import require
from core.tools.operation_control import OperationControl
from memory.repositories.tasks import encode


def owner(context, session_id):
    return {'project_id': context.artifacts._owner, 'session_id': session_id,
            'task_id': context.task_service.current(session_id)['task_id']}


def identity(owner, name):
    return 'workflow-' + hashlib.sha256(encode([owner, name]).encode()).hexdigest()


def load(context, who, name):
    row = context.operation_ledger.store.operation_get(identity(who, name))
    require(row is not None and row['tool'] == 'browser_workflow', 'workflow_not_found')
    require(row['facts']['owner'] == who, 'workflow_scope_denied')
    return row


def save(context, who, name, definition, run_id):
    ledger = context.operation_ledger
    oid = identity(who, name)
    digest = args_hash('browser_workflow', definition, [])
    old = ledger.store.operation_get(oid)
    if old:
        require(old['args_hash'] == digest and old['facts']['owner'] == who, 'workflow_definition_changed')
        return old
    control = OperationControl(run_id)
    control.operation_id = oid
    ledger.bind(control, context.registry.get('browser_workflow'), digest)
    count = len(definition['urls'] if definition['mode'] == 'collect' else definition['steps'])
    control.facts = {'before': {}, 'owner': who, 'definition': definition, 'workflow_id': name,
        'permission': 'METADATA_ONLY', 'approval_source': 'not_authorization',
        'steps': [{'state': 'pending', 'attempts': 0, 'operation_id': oid + ':' + str(i)} for i in range(count)]}
    ledger.prepare(control)
    ledger.executing(control)
    return ledger.store.operation_get(oid)


def refresh(context, row):
    for step in row['facts']['steps']:
        if not step['attempts']:
            continue
        child = context.operation_ledger.store.operation_get(step['operation_id'])
        if child and child['tool'] in {'browser_click', 'browser_fill'}:
            step['state'] = child['state']
            step['proof'] = child['facts'].get('business_proof')
    return row


def persist(context, row):
    store = context.operation_ledger.store
    store._owner.assert_held()
    with store.transaction():
        current = store.operation_get(row['operation_id'])
        require(current['fencing_generation'] == store.fencing_generation, 'workflow_restart_requires_review')
        store._conn.execute('UPDATE tool_operations SET facts=?,updated_at=? WHERE operation_id=?',
                            (encode(row['facts']), store._now(), row['operation_id']))
        if all(step['state'] == 'committed' for step in row['facts']['steps']):
            store.operation_finish(row['operation_id'], 'committed',
                {'kind': 'workflow_steps', 'child_operations': [s['operation_id'] for s in row['facts']['steps']]},
                row['facts'])


def report(row):
    steps = row['facts']['steps']
    done = [i for i, step in enumerate(steps) if step['state'] == 'committed']
    unfinished = [i for i in range(len(steps)) if i not in done]
    state = 'completed' if not unfinished else 'manual_required' if any(
        s['attempts'] and s['state'] != 'committed' for s in steps) else 'ready'
    if state == 'ready' and any(s.get('question_id') and not s['attempts'] for s in steps):
        state = 'waiting_input'
    results, missing, by_value = [], [], {}
    for i, step in enumerate(steps):
        if step.get('record'):
            record = step['record']
            key = encode(record['values'])
            if key in by_value:
                by_value[key]['sources'].append(record['source'])
            else:
                item = {'values': record['values'], 'sources': [record['source']]}
                by_value[key] = item
                results.append(item)
            missing.extend({'page': i, 'field': k, 'source': record['source']} for k, v in record['values'].items() if v is None)
        elif step['attempts'] and row['facts']['definition']['mode'] == 'collect':
            missing.append({'page': i, 'field': '*', 'reason': step.get('error')})
    return {'workflow_id': row['facts']['workflow_id'], 'state': state, 'definition': row['facts']['definition'],
        'done': done, 'not_done': unfinished, 'steps': steps, 'records': results, 'missing': missing,
        'pages_planned': len(steps) if row['facts']['definition']['mode'] == 'collect' else 1,
        'pages_attempted': sum(s['attempts'] for s in steps) if row['facts']['definition']['mode'] == 'collect' else None,
        'duplicates_removed': sum('record' in s for s in steps) - len(results),
        'retry_limit': 0, 'instructions_executable': False,
        'partial': bool(unfinished or missing or row['facts']['definition']['mode'] == 'collect'),
        'collection_scope': 'bounded visible snapshots; fields truncated to 256 characters'}


def output(context, row):
    result = report(row)
    text = encode(result)
    if len(text.encode('utf-8')) <= 48000:
        return text
    artifact = context.artifacts.put(text.encode('utf-8'), source_ref='workflow:' + row['operation_id'])
    summary = {k: result[k] for k in ('workflow_id', 'state', 'done', 'not_done', 'pages_planned',
        'pages_attempted', 'duplicates_removed', 'retry_limit', 'instructions_executable', 'partial', 'collection_scope')}
    return encode({**summary, 'artifact_ref': artifact.uri, 'pagination': 'read_artifact for full bounded results',
                   'record_count': len(result['records']), 'missing_count': len(result['missing'])})
