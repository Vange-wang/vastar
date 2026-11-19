"""Explicit delivery connects current-run ledger facts to the existing Validator."""
import hashlib
import json
from pathlib import Path

from core.harness.validators import Validator


def _operation(store, task, step, run, operation_id):
    op = store.operation_get(operation_id)
    if not op or (op['task_id'], op['step_id'], op['run_id']) != (
            task['task_id'], step['step_id'], run['run_id']):
        raise ValueError('旧回单或其他任务/步骤/Run 的证据不能作为本轮成果')
    if op['state'] != 'committed' or not op['receipt']:
        raise ValueError('操作尚未确认提交，不能交付')
    return op


def _evidence(store, task, step, run, supplied):
    ids = [r[0] for r in store._conn.execute(
        'SELECT operation_id FROM tool_operations WHERE run_id=? AND state=? ORDER BY rowid DESC',
        (run['run_id'], 'committed'))]
    evidence, outputs = {}, []
    for key in step['acceptance_ids']:
        criterion = task['acceptance'][key]
        if not isinstance(criterion, dict) or criterion.get('kind') not in {'file_hash', 'command'}:
            raise ValueError('自动交付仅支持 file_hash/command；其他验收请显式 /task validate')
        given = supplied.get(key, {})
        if set(given) - {'operation_id'}:
            raise ValueError('交付须引用本轮 operation_id，不能用旧 artifact 或模型自报替代')
        candidates = [given['operation_id']] if 'operation_id' in given else ids
        matched = None
        for oid in candidates:
            op = _operation(store, task, step, run, oid)
            if criterion['kind'] == 'file_hash':
                path = Path(criterion['path'])
                if not path.is_absolute():
                    raise ValueError('成果路径须为绝对路径')
                after = op['facts'].get('after', {}).get(str(path), {})
                if op['classification'] == 'read' or after.get('sha256') != criterion['sha256']:
                    continue
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
                if actual != criterion['sha256']:
                    raise ValueError('成果内容已变化，SHA256 与本轮回单不符')
                outputs.append({'path': str(path), 'sha256': actual, 'operation_id': oid})
            else:
                facts = op['receipt'].get('audit', {}).get('command_result', {})
                if facts.get('argv') != criterion['argv']:
                    continue
            matched = {'operation_id': oid}
            break
        if matched is None:
            raise ValueError(f'{key} 缺少本轮已提交的成果证据；文件不存在或尚未执行，不交付')
        evidence[key] = matched
    return evidence, outputs


def deliver_task(coord, tail=''):
    """One explicit command, one selected step; no scheduling or autonomous loop."""
    service, store = coord.task_service, coord.task_service.store
    task = service.current(coord.session_id)
    steps = [s for s in task['steps'] if s['status'] in {'RUNNING', 'VERIFYING'}]
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
    evidence, outputs = _evidence(store, task, step, dict(row), supplied)
    recovery = None
    runtime = store._conn.execute('SELECT recovery_error FROM task_runtime WHERE task_id=?',
                                  (task['task_id'],)).fetchone()
    if row['terminal_state'] == 'INTERRUPTED' or (runtime and runtime[0]):
        from core.harness.recovery_delivery import recover_delivery
        recovery = recover_delivery(coord, task, row)
    artifacts = coord.task_tools_context.artifacts
    manifest = {'task_id': task['task_id'], 'run_id': row['run_id'],
                'outputs': outputs}
    if recovery:
        manifest['recovery'] = recovery
    # Publish evidence before changing Task status: storage failure cannot complete a Task.
    ref = artifacts.put(json.dumps(manifest, ensure_ascii=False).encode('utf-8'),
                        source_ref=f"task:{task['task_id']}/run:{row['run_id']}")
    result = Validator(store, artifacts).validate_step(service, coord.session_id, step['step_id'], evidence)
    if not result['passed'] or result['status'] != 'COMPLETED':
        return {'validation': result, 'delivery': None}
    from core.harness.task_commands import checkpoint_command
    checked = checkpoint_command(coord, result)
    if isinstance(checked, dict) and checked.get('checkpoint_status'):
        return {'validation': checked, 'delivery': None}
    return {'validation': result, 'delivery': {**manifest, 'validation_id': result['validation_id'],
                                              'artifact_ref': ref.uri, 'sha256': ref.hash}}
