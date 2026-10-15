"""S06 durable scope and machine evidence; model claims cannot finish a process."""
import hashlib
import json

from core.harness.operations import args_hash
from core.skills.code_change_snapshot import require
from core.tools.operation_control import OperationControl
from memory.repositories.tasks import encode


def identity(context, session, name):
    owner = [context.artifacts._owner, session, context.task_service.current(session)['task_id']]
    return 'code-' + hashlib.sha256(encode([owner, name]).encode()).hexdigest(), owner


def load(context, session, name):
    oid, owner = identity(context, session, name)
    row = context.operation_ledger.store.operation_get(oid)
    require(row and row['tool'] == 'code_change' and row['facts']['owner'] == owner, 'code_scope_missing')
    require(row['fencing_generation'] == context.operation_ledger.store.fencing_generation, 'code_restart_review_required')
    return row


def save(context, session, name, definition, baseline, hashes, run_id):
    oid, owner = identity(context, session, name)
    ledger = context.operation_ledger
    require(ledger.store.operation_get(oid) is None, 'code_scope_already_locked')
    control = OperationControl(run_id)
    control.operation_id = oid
    ledger.bind(control, context.registry.get('code_change'), args_hash('code_change', definition, []))
    control.facts = {'before': {}, 'owner': owner, 'definition': definition, 'baseline': baseline,
                     'hashes': hashes, 'attempts': [], 'edits': [], 'permission': 'METADATA_ONLY'}
    ledger.prepare(control)
    ledger.executing(control)
    return ledger.store.operation_get(oid)


def persist(context, row):
    store = context.operation_ledger.store
    with store.transaction():
        require(row['fencing_generation'] == store.fencing_generation, 'code_restart_review_required')
        store._conn.execute('UPDATE tool_operations SET facts=?,updated_at=? WHERE operation_id=?',
                            (encode(row['facts']), store._now(), row['operation_id']))


def output(context, job, owner):
    entry = context.process_service.lookup(job['job_id'], owner, job['started_at'])
    info, stream = entry['info'], entry['output']
    require(info['status'] not in {'preparing', 'started'}, 'code_process_still_running')
    snapshot = stream.snapshot()
    require(not snapshot.get('incomplete') and not snapshot.get('limit_reached'), 'code_output_incomplete')
    data = b''
    for chunk in snapshot.get('chunks', []):
        if chunk['stream'] == 'stdout':
            _, body = context.artifacts.get(chunk['artifact_id'], chunk['version'])
            data += body
    return dict(info), data.decode('utf-8', errors='strict'), snapshot


def passed(info):
    return info.get('status') == 'completed' and info.get('exit_code') == 0 and info.get('completed_successfully') is True


def classify(info, baseline=None):
    if passed(info):
        return 'passed'
    if info.get('status') == 'failed':
        return 'environment_or_probe_failure'
    if baseline and passed(baseline):
        return 'possible_regression'
    return 'pre_existing_or_environment' if baseline else 'unclassified_failure'


def probe(text):
    item = json.loads(text.splitlines()[0])
    require(set(item) == {'base_revision', 'dirty_raw', 'diff'}, 'code_probe_invalid')
    require(len(item['base_revision']) == 40 and all(c in '0123456789abcdef' for c in item['base_revision']),
            'code_revision_invalid')
    return item
