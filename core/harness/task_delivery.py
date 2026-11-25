"""Explicit delivery connects current-run ledger facts to the existing Validator."""
import hashlib
import json
from pathlib import Path

from core.harness.task_completion import recorded_results
from core.harness.validators import Validator


def _operation(store, task, step, run, operation_id):
    op = store.operation_get(operation_id)
    if not op or (op['task_id'], op['step_id'], op['run_id']) != (
            task['task_id'], step['step_id'], run['run_id']):
        raise ValueError('旧回单或其他任务/步骤/Run 的证据不能作为本轮成果')
    if op['state'] != 'committed' or not op['receipt']:
        raise ValueError('操作尚未确认提交，不能交付')
    return op


def _written_file(store, task, step, run, criterion, evidence):
    path = Path(criterion['path'])
    if not path.is_absolute() or not isinstance(evidence, dict):
        return None
    # Select the last write to this path first. A later mismatching or missing receipt
    # cannot turn an older matching write into the final effect of this run.
    row = store._conn.execute(
        """SELECT operation_id FROM tool_operations AS op
           WHERE task_id=? AND step_id=? AND run_id=? AND state='committed' AND classification!='read'
           AND (EXISTS (SELECT 1 FROM json_each(op.facts,'$.after') WHERE key=?)
                OR EXISTS (SELECT 1 FROM json_each(op.facts,'$.expected') WHERE key=?))
           ORDER BY updated_at DESC, op.rowid DESC LIMIT 1""",
        (task['task_id'], step['step_id'], run['run_id'], str(path), str(path))).fetchone()
    if not row or (evidence and evidence != {'operation_id': row[0]}):
        return None
    op = store.operation_get(row[0])
    if not op or not op['receipt'] or op['receipt_ref'] != 'receipt:' + row[0]:
        return None
    after = op['facts'].get('after', {}).get(str(path), {})
    if after.get('exists') is not True or after.get('sha256') != criterion['sha256']:
        return None
    return {'path': str(path), 'sha256': criterion['sha256'], 'operation_id': op['operation_id']}


def _recorded_file(store, task, step, criterion, evidence, *, rechecked):
    # A manual file_hash validation can legitimately attest to a pre-existing input.
    latest = store._conn.execute(
        'SELECT run_id,terminal_state FROM runs WHERE task_id=? AND step_id=? ORDER BY rowid DESC LIMIT 1',
        (task['task_id'], step['step_id'])).fetchone()
    if not latest or latest['terminal_state'] not in {'COMPLETED', 'INTERRUPTED'}:
        return None
    output = _written_file(store, task, step, latest, criterion, evidence)
    if output and not rechecked:
        try:
            with Path(output['path']).open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != output['sha256']:
                    return None
        except OSError:
            return None
    return output


def _skill_evidence(validator, task, step, run, criterion, given):
    tool = 'code_change' if criterion['kind'] == 'code_check' else criterion.get('tool')
    if tool not in ('code_change', 'generate_report', 'analyze_spreadsheet'):
        raise ValueError('未知的成果工具')
    ids = [given['operation_id']] if given else [row[0] for row in validator.store._conn.execute(
        'SELECT operation_id FROM tool_operations WHERE task_id=? AND step_id=? AND run_id=? AND tool=? ORDER BY rowid DESC',
        (task['task_id'], step['step_id'], run['run_id'], tool))]
    reason = '未找到对应操作'
    for oid in ids:
        checked = validator.check_step_result(task, step, criterion, {'operation_id': oid})
        if checked['passed']:
            return checked['evidence'], checked['outputs']
        reason = checked['reason']
    raise ValueError(f'缺少已验证的交付成果：{reason}')


def _evidence(store, task, step, run, supplied, validator):
    ids = [r[0] for r in store._conn.execute(
        'SELECT operation_id FROM tool_operations WHERE run_id=? AND state=? ORDER BY rowid DESC',
        (run['run_id'], 'committed'))]
    evidence, outputs = {}, []
    for key in step['acceptance_ids']:
        criterion = task['acceptance'][key]
        given = supplied.get(key, {})
        if (not isinstance(given, dict) or set(given) - {'operation_id'}
                or given and not isinstance(given['operation_id'], str)):
            raise ValueError('交付须引用本轮 operation_id，不能用旧 artifact 或模型自报替代')
        if isinstance(criterion, dict) and criterion.get('kind') in {'deliverable', 'code_check'}:
            evidence[key], current = _skill_evidence(validator, task, step, run, criterion, given)
            outputs.extend(current)
            continue
        if not isinstance(criterion, dict) or criterion.get('kind') not in {'file_hash', 'command'}:
            raise ValueError('其他验收请显式 /task validate')
        matched = None
        if criterion['kind'] == 'file_hash':
            output = _written_file(store, task, step, run, criterion, given)
            if output:
                with Path(output['path']).open('rb') as stream:
                    if hashlib.file_digest(stream, 'sha256').hexdigest() != output['sha256']:
                        raise ValueError('成果内容已变化，SHA256 与本轮回单不符')
                outputs.append(output)
                matched = {'operation_id': output['operation_id']}
        else:
            candidates = [given['operation_id']] if given else ids
            for oid in candidates:
                op = _operation(store, task, step, run, oid)
                facts = op['receipt'].get('audit', {}).get('command_result', {})
                if facts.get('argv') != criterion['argv']:
                    continue
                matched = {'operation_id': oid}
                break
        if matched is None:
            raise ValueError(f'{key} 缺少本轮已提交的成果证据；文件不存在或尚未执行，不交付')
        evidence[key] = matched
    return evidence, outputs


def task_outputs(coord, *, recheck=True):
    """List validated files, including earlier steps, without changing task state."""
    service = coord.task_service
    task = service.current(coord.session_id)
    validator = Validator(service.store, coord.task_tools_context.artifacts)
    outputs, problems = [], []
    for step in task['steps']:
        if step['status'] != 'COMPLETED':
            continue
        recorded = recorded_results(service.store, task['task_id'], step)
        for key in step['acceptance_ids']:
            old = recorded.get(key, {})
            evidence = old.get('evidence', {})
            if not old.get('passed'):
                problems.append({'step_id': step['step_id'], 'criterion': key, 'reason': '缺少验收记录'})
                continue
            criterion = task['acceptance'][key]
            checked = validator.check_step_result(task, step, criterion, evidence) if recheck else old
            if not checked['passed']:
                problems.append({'step_id': step['step_id'], 'criterion': key,
                                 'reason': checked.get('reason', '重新核对失败')})
            elif criterion['kind'] == 'file_hash':
                output = _recorded_file(service.store, task, step, criterion, evidence, rechecked=recheck)
                if output is not None:
                    outputs.append(output)
            else:
                outputs.extend(checked.get('outputs', []))
    return {'task_id': task['task_id'], 'status': task['status'], 'outputs': outputs, 'problems': problems}


def deliver_task(coord, tail=''):
    """One explicit command, one selected step; no scheduling or autonomous loop."""
    service, store = coord.task_service, coord.task_service.store
    task = service.current(coord.session_id)
    steps = [s for s in task['steps'] if s['status'] in {'RUNNING', 'VERIFYING'}]
    if not steps and task['status'] not in {'BLOCKED', 'CANCELLED', 'COMPLETED'}:
        # A finished native process keeps an unknown generic side-effect receipt.
        # Only the coding verifier can bind it to an approved offline snapshot.
        steps = [s for s in task['steps'] if s['status'] == 'BLOCKED' and s['acceptance_ids']
                 and all(isinstance(task['acceptance'][k], dict) and task['acceptance'][k].get('kind') == 'code_check'
                         for k in s['acceptance_ids'])]
    if len(steps) != 1:
        raise ValueError('交付要求唯一已执行且待验收步骤；/task status 查看')
    step = steps[0]
    row = store._conn.execute('SELECT * FROM runs WHERE task_id=? AND step_id=? ORDER BY rowid DESC LIMIT 1',
                              (task['task_id'], step['step_id'])).fetchone()
    if not row or row['terminal_state'] not in {'COMPLETED', 'INTERRUPTED'}:
        raise ValueError('本轮未正常结束或已取消；不验收、不产生交付引用')
    supplied = json.loads(tail) if tail.strip() else {}
    if not isinstance(supplied, dict) or set(supplied) - set(step['acceptance_ids']):
        raise ValueError('交付证据必须为当前步骤验收键的对象')
    validator = Validator(store, coord.task_tools_context.artifacts)
    evidence, outputs = _evidence(store, task, step, dict(row), supplied, validator)
    recovery = None
    runtime = store._conn.execute('SELECT recovery_error FROM task_runtime WHERE task_id=?',
                                  (task['task_id'],)).fetchone()
    if row['terminal_state'] == 'INTERRUPTED' or (runtime and runtime[0]):
        from core.harness.recovery_delivery import recover_delivery
        recovery = recover_delivery(coord, task, row)
    artifacts = coord.task_tools_context.artifacts
    # Re-use the last validated manifest; validate_step reopens all earlier evidence
    # before committing COMPLETED, even when an earlier output disappeared.
    prior = task_outputs(coord, recheck=False)
    manifest = {'task_id': task['task_id'], 'run_id': row['run_id'],
                'outputs': prior['outputs'] + outputs}
    if recovery:
        manifest['recovery'] = recovery
    # Publish evidence before changing Task status: storage failure cannot complete a Task.
    last = all(s['step_id'] == step['step_id'] or s['status'] == 'COMPLETED' for s in task['steps'])
    ref = (artifacts.put(json.dumps(manifest, ensure_ascii=False).encode('utf-8'),
                         source_ref=f"task:{task['task_id']}/run:{row['run_id']}") if last else None)
    result = validator.validate_step(service, coord.session_id, step['step_id'], evidence)
    if not result['passed']:
        return {'validation': result, 'delivery': None}
    from core.harness.task_commands import checkpoint_command
    checked = checkpoint_command(coord, result)
    if isinstance(checked, dict) and checked.get('checkpoint_status'):
        return {'validation': checked, 'delivery': None}
    if result['status'] != 'COMPLETED' or ref is None:
        return {'validation': result, 'delivery': None, 'step_outputs': outputs}
    return {'validation': result, 'delivery': {**manifest, 'validation_id': result['validation_id'],
                                              'artifact_ref': ref.uri, 'sha256': ref.hash}}
