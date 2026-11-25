"""S04 analysis lifecycle and receipts through existing Task-owned process service."""
import asyncio
import json
import re
import uuid
from dataclasses import asdict

from core.harness.operations import args_hash
from core.tools.document_results import collect
from core.tools.operation_receipt import ToolOutcome
from core.tools.operation_runner import drain, thread_call
from core.tools.permissions import resolve_read_path
from core.tools.process_tools import start_approved
from core.tools.sheet_adapter import LIMITS, SheetPayload, options
from core.tools.sheet_results import legacy_receipt, publish, retained_source


def _finished_sheet(row, document):
    proof = row['facts'].get('sheet')
    receipt = row.get('receipt')
    if not isinstance(proof, dict) or not isinstance(receipt, dict) or not isinstance(receipt.get('audit'), dict):
        return False
    audit = receipt['audit']
    status = proof.get('status')
    hashes = proof.get('hashes')
    if (status not in ('completed', 'partial', 'incomplete') or proof.get('method') != document
            or not isinstance(hashes, dict) or hashes.get('unchanged') is not True
            or any(hashes.get(k) != document.get('source_hash') for k in ('before', 'after', 'copy_hash'))
            or not isinstance(proof.get('artifact_refs'), dict) or not proof['artifact_refs'].get('analysis.json')
            or receipt.get('name') != 'analyze_spreadsheet' or type(audit.get('sheet_complete')) is not bool
            or audit.get('operation_id') != row['operation_id'] or audit.get('owner') != row['task_id']
            or receipt.get('status') != ('failed' if status == 'incomplete' else 'executed')):
        return False
    raw = receipt.get('result')
    if not isinstance(raw, str) or len(raw) > LIMITS['output_bytes']:
        return False
    try:
        return json.loads(raw) == proof
    except (ValueError, RecursionError):
        return False


def replay_after_worker_update(ledger, operation_id, run_id, args, payload):
    """Only read an existing final receipt. The live payload and new-intent digest are never changed."""
    store = ledger.store
    row = store.operation_get(operation_id)
    if (not row or row['state'] != 'committed' or row['tool'] != 'analyze_spreadsheet'
            or row['run_id'] != run_id or row['task_id'] != args['task_id']
            or row['receipt_ref'] != 'receipt:' + operation_id or not isinstance(row.get('facts'), dict)
            or row.get('approval_ref') != {'permission': 'ALLOW', 'approval_source': 'user_allow'}):
        return None
    document = row['facts'].get('document')
    if not isinstance(document, dict):
        return None
    current = args['payload']
    workers = document.get('worker_sha256')
    if (not isinstance(workers, dict) or not workers or workers == current['worker_sha256']
            or workers.keys() != current['worker_sha256'].keys()
            or any(not isinstance(v, str) or not re.fullmatch(r'[0-9a-f]{64}', v) for v in workers.values())
            or document != {**current, 'worker_sha256': workers} or not _finished_sheet(row, document)):
        return None
    historical = args_hash(row['tool'], {**args, 'payload': document}, [])
    if historical != row['args_hash']:
        return None
    # A user can leave an approval prompt open while the source file changes.
    payload.verify_unchanged()
    with store._lock:
        run = store._conn.execute('SELECT step_id FROM runs WHERE run_id=? AND task_id=?',
                                  (run_id, row['task_id'])).fetchone()
        if not run or not row['step_id'] or row['step_id'] != run[0]:
            return None
        if store.operation_get(operation_id) != row:
            return None
        outcome = ledger.replay(operation_id, historical, run_id)
        outcome.audit['historical_worker_replay'] = True
        return outcome


def _analysis_status(outcome, result):
    status = result['status']
    outcome.audit.update(sheet_status=status, sheet_complete=status == 'completed')
    if status in ('incomplete', 'unverified'):
        # Older receipts kept the worker error in limitations and the same completion flag for every state.
        reason = result.get('error') or next(filter(None, result.get('limitations', [])), '分析未完成')
        outcome.status = 'unknown' if status == 'unverified' else 'failed'
        outcome.reason = reason if status == 'unverified' else '未完成分析：' + reason
        if result.get('diagnostics_path'):
            outcome.reason += '。诊断记录：' + result['diagnostics_path']
        if result.get('retained_source'):
            outcome.reason += '。暂存副本：' + result['retained_source']
    return outcome


def _incomplete(outcome, payload, **details):
    retained = retained_source(payload)
    if retained:
        outcome.reason += '。暂存副本：' + retained
    outcome.result = json.dumps({'status': 'incomplete', 'retained_source': retained, **details}, ensure_ascii=False)
    outcome.redact_result_preview = True
    return outcome


class SheetService:
    def __init__(self, context):
        self.context = context

    async def execute(self, spec, args, session_id, run_id, operation_id, cancel, emitter):
        payload = None
        try:
            if spec.name == 'sheet_capabilities':
                return ToolOutcome(spec.name, 'executed', result=json.dumps({
                    'formats': ['UTF-8 CSV', 'stdlib OOXML XLSX'], 'level': 'bounded_partial',
                    'formula_execution': False, 'external_links': 'never fetched', 'limits': LIMITS,
                    'isolation': self.context.execution_sandbox.preflight()}, ensure_ascii=False))
            settings = options(args)
            path = resolve_read_path(self.context.cwd or self.context.project_root, args['path'])
            staging = self.context.artifacts._require_dir().parent / 'sheet-staging' / uuid.uuid4().hex
            payload = await thread_call(SheetPayload, path, staging, settings)
            owner = self.context.task_service.current(session_id)['task_id']
            outcome = await start_approved(self.context, spec, {'script': payload.script}, owner, session_id,
                                           run_id, operation_id, cancel, emitter, payload=payload)
            if 'sheet_complete' in outcome.audit:
                known = outcome.audit.get('sheet_status')
                if known and not outcome.audit.get('historical_worker_replay'):
                    return outcome
                result = await thread_call(legacy_receipt, self.context.artifacts, payload, outcome.result)
                if known and known == result['status']:
                    return outcome
                outcome.result = json.dumps(result, ensure_ascii=False)
                return _analysis_status(outcome, result)
            if outcome.status != 'executed':
                return _incomplete(outcome, payload, process=outcome.audit)
            return await self._finish(spec, payload, outcome.audit, cancel)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return _incomplete(ToolOutcome(spec.name, 'failed', '未完成分析：' + str(exc), origin='sheet_gate'), payload)

    async def _finish(self, spec, payload, info, cancel):
        service = self.context.process_service
        entry = service.lookup(info['job_id'], info['owner'], info['started_at'])
        if entry.get('done') is None:
            return _incomplete(ToolOutcome(spec.name, 'unknown', '缺少原进程句柄；先 reconcile，不自动重试'), payload)
        try:
            await asyncio.shield(asyncio.get_running_loop().run_in_executor(None, entry['done'].wait))
        except asyncio.CancelledError:
            entry['cancel'].set()
            await drain(asyncio.get_running_loop().run_in_executor(None, entry['done'].wait))
            raise
        final = dict(entry['info'])
        if cancel.is_set() or not self.context.registry.is_enabled(spec.name) or final['status'] != 'completed':
            failed = ToolOutcome(spec.name, 'failed', f"隔离分析未完成（{final['status']}）", audit=final)
            return _incomplete(failed, payload, process=final,
                               diagnostics=bytes(entry['output'].stderr).decode('utf-8', errors='replace'))
        try:
            raw = await thread_call(collect, service, final, limit=LIMITS['output_bytes'])
            result = await thread_call(publish, self.context.artifacts, payload, json.loads(raw))
        except (ValueError, KeyError) as exc:
            failed = _incomplete(ToolOutcome(spec.name, 'failed', str(exc), audit=final), payload)
            service.store.operation_finish(final['operation_id'], 'not_committed', asdict(failed),
                                           {**entry['control'].facts, 'sheet_validation': 'failed'})
            return failed
        outcome = _analysis_status(ToolOutcome(spec.name, 'executed', result=json.dumps(result, ensure_ascii=False),
                                               audit=final, redact_result_preview=True), result)
        try:
            service.store.operation_finish(final['operation_id'], 'committed', asdict(outcome),
                                           {**entry['control'].facts, 'sheet': result})
        except Exception:
            return ToolOutcome(spec.name, 'unknown', '产物已发布但回执未确认；先 reconcile', result=outcome.result)
        return outcome
