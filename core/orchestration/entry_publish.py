"""Hash-checked host integration and durable publication, never model completion claims."""
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from core.harness.operations import OperationLedger, args_hash
from core.orchestration.integrator import Integrator
from core.tools.operation_control import OperationControl
from core.tools.operation_receipt import ToolOutcome
from core.tools.operation_resources import file_facts


def accept_child(item):
    def accept(envelope, result):
        if not all(fact in result.summary for fact in item['facts']):
            return False
        if not all(Path(p).read_text(encoding='utf-8') == text for p, text in item['reads']):
            return False
        if set(result.evidence_refs) != set(envelope.input_refs):
            return False
        if item['write']:
            return (set(result.changed_paths) == set(envelope.write_scope)
                    and all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == item['sha256']
                            for p in result.changed_paths))
        return not result.changed_paths
    return accept


def publish(coord, scheduler, items, results, run_id, traces):
    plans = scheduler.plans
    plan = plans.current()
    snapshot = plans.snapshot()
    if any(snapshot['records'][e.step_id]['state'] != 'reported' for e in plan.steps):
        raise ValueError('team_incomplete_children; parent remains incomplete')
    # Recheck all source facts and summaries after the workers drained.
    for item, envelope in zip(items, plan.steps, strict=True):
        result = snapshot['records'][envelope.step_id]['result']
        if (not all(f in result['summary'] for f in item['facts'])
                or any(Path(p).read_text(encoding='utf-8') != t for p, t in item['reads'])):
            raise ValueError('team_evidence_changed_or_facts_lost')
    publication = None
    if scheduler.workspaces:
        publication = _integrate(coord, scheduler, items, run_id)
    for envelope in plan.steps:
        plans.accept_step(envelope.step_id, expected_revision=plan.revision,
                          verify=lambda value: value['status'] == 'completed')
    manifest = dict(task_id=plan.task_id, run_id=run_id, status='integrated',
                    children={key: asdict(value) for key, value in results.items()}, publication=publication,
                    traces=traces, sources={e.ref: asdict(e) for step in plan.steps
                        for e in scheduler.store.select(step, scheduler.parent.evidence_refs, ())})
    ref = coord.task_tools_context.artifacts.put(json.dumps(manifest, ensure_ascii=False).encode('utf-8'),
                                                 source_ref=f'task:{plan.task_id}/run:{run_id}')
    return {**manifest, 'artifact_ref': ref.uri,
            'note': '子成果已整合；文件成果用 /task deliver，其他验收用 /task validate；父任务尚未宣告完成。'}


def _integrate(coord, scheduler, items, run_id):
    manager = scheduler.workspaces
    rows = [r for r in manager.state()['workspaces'].values()
            if r['envelope']['task_id'] == scheduler.parent.task_id]
    expected = {Path(i['write']).relative_to(manager.root).as_posix(): i['sha256']
                for i in items if i['write']}
    paths = [manager.root / p for p in expected]
    control = OperationControl(run_id)
    control.facts = {'before': file_facts(paths), 'permission': 'ALLOW',
                     'approval_source': 'accepted_task_and_host_auto',
                     'expected': {str(p): {'exists': True, 'sha256': expected[p.relative_to(manager.root).as_posix()]}
                                  for p in paths}}
    ledger = OperationLedger(coord._store)
    spec = SimpleNamespace(name='team_integrate', effect='write', retryable=False)
    ledger.bind(control, spec, args_hash(spec.name, expected, paths))
    ledger.prepare(control)
    ledger.executing(control)
    envelopes = {e.step_id: e for e in scheduler.plans.current().steps}

    def review(view):
        return all(p in view and hashlib.sha256(view[p]).hexdigest() == digest for p, digest in expected.items())

    def validate(directory):
        scheduler.identity_check(None, None)
        return all(hashlib.sha256((directory / p).read_bytes()).hexdigest() == digest
                   for p, digest in expected.items())

    try:
        output = control.commit(lambda: Integrator(manager).integrate(
            [r['id'] for r in rows], envelopes=envelopes.__getitem__, reviewer=review, validate=validate))
        control.facts['after'] = file_facts(paths)
        ledger.finish(control, ToolOutcome(spec.name, 'executed', audit={'integration': output}))
        return {**output, 'operation_id': control.operation_id}
    except BaseException:
        control.facts['after'] = file_facts(paths)
        ledger.finish(control, ToolOutcome(spec.name, 'failed', reason='integration failed; reconcile if unknown'))
        raise
