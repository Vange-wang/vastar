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


def read_review(artifacts, row, reference=None):
    """Reopen the exact source-bound report, checking its contents and original owner."""
    recorded = row['facts'].get('review')
    require(isinstance(recorded, dict) and isinstance(recorded.get('artifact_ref'), str),
            'code_delivery_report_required')
    ref = recorded['artifact_ref']
    require(reference is None or reference == ref, 'code_delivery_report_mismatch')
    aid, version, _ = artifacts._parse_ref(ref)
    meta, raw = artifacts.get(aid, version)
    require(meta['uri'] == ref and meta['source_ref'] == row['operation_id'] + ':review'
            and meta['hash'] == recorded.get('sha256'), 'code_delivery_report_mismatch')
    report = json.loads(raw)
    require(isinstance(report, dict) and report.get('schema') == 'vastar.code-review.v1'
            and report.get('scope_operation_id') == row['operation_id']
            and all(k in report and report[k] == row[k] for k in ('task_id', 'step_id', 'run_id')),
            'code_delivery_report_mismatch')
    return report, raw, ref


def save_review(context, row, review):
    payload = encode(review).encode('utf-8')
    if 'review' in row['facts']:
        _, previous, reference = read_review(context.artifacts, row)
        if previous == payload:
            return reference
    artifact = context.artifacts.put(payload, source_ref=row['operation_id'] + ':review')
    row['facts']['review'] = {'artifact_ref': artifact.uri, 'sha256': artifact.hash}
    persist(context, row)
    return artifact.uri


def output(context, job, owner):
    entry = context.process_service.lookup(job['job_id'], owner, job['started_at'])
    info, stream = entry['info'], entry['output']
    require(info['status'] not in {'preparing', 'started'}, 'code_process_still_running')
    snapshot = stream.snapshot()
    require(not snapshot.get('incomplete') and not snapshot.get('limit_reached'), 'code_output_incomplete')
    chunks = []
    for chunk in snapshot.get('chunks', []):
        if chunk['stream'] == 'stdout':
            _, body = context.artifacts.get(chunk['artifact_id'], chunk['version'])
            chunks.append(body)
    # Commands may emit local encodings or binary output. Preserve the text
    # interface for existing callers and let every byte round-trip to the artifact.
    return dict(info), b''.join(chunks).decode('utf-8', errors='surrogateescape'), snapshot


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


def probe(stdout):
    # The worker writes one UTF-8 JSON header; the command owns all following bytes.
    header = stdout.partition('\n')[0].encode('utf-8')
    item = json.loads(header)
    require(isinstance(item, dict) and isinstance(item.get('dirty_raw'), str) and isinstance(item.get('diff'), str),
            'code_probe_invalid')
    fields = {'base_revision', 'dirty_raw', 'diff'}
    rev = item.get('base_revision')
    if rev is None:
        require(set(item) == fields | {'unborn_ref'}, 'code_probe_invalid')
        ref = item['unborn_ref']
        require(isinstance(ref, str) and ref.startswith('refs/heads/') and len(ref) > len('refs/heads/')
                and all(ord(c) > 32 and ord(c) != 127 for c in ref),
                'code_unborn_ref_invalid')
    else:
        require(set(item) == fields, 'code_probe_invalid')
        require(isinstance(rev, str) and len(rev) == 40 and rev != '0' * 40 and all(c in '0123456789abcdef' for c in rev),
                'code_revision_invalid')
    return item


def revision_identity(baseline):
    return {k: baseline[k] for k in ('base_revision', 'unborn_ref') if k in baseline}


def baseline_probe(baseline):
    return {**revision_identity(baseline), 'dirty_raw': baseline['dirty_raw'], 'diff': baseline['diff']}
