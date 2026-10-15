"""S04 analysis lifecycle and receipts through existing Task-owned process service."""
import asyncio
import json
import uuid
from dataclasses import asdict

from core.tools.document_results import collect
from core.tools.operation_receipt import ToolOutcome
from core.tools.operation_runner import drain, thread_call
from core.tools.permissions import resolve_read_path
from core.tools.process_tools import start_approved
from core.tools.sheet_adapter import LIMITS, SheetPayload, options
from core.tools.sheet_results import publish


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
            if outcome.audit.get('sheet_complete'):
                return outcome
            if outcome.status != 'executed':
                return ToolOutcome(spec.name, outcome.status, outcome.reason, result=json.dumps({
                    'status': 'incomplete', 'retained_source': str(payload.frozen), 'process': outcome.audit}),
                    audit=outcome.audit, redact_result_preview=True)
            return await self._finish(spec, payload, outcome.audit, cancel)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return ToolOutcome(spec.name, 'failed', '未完成分析：' + str(exc), result=json.dumps({
                'status': 'incomplete', 'retained_source': str(payload.frozen) if payload else None}),
                origin='sheet_gate', redact_result_preview=True)

    async def _finish(self, spec, payload, info, cancel):
        service = self.context.process_service
        entry = service.lookup(info['job_id'], info['owner'], info['started_at'])
        if entry.get('done') is None:
            return ToolOutcome(spec.name, 'unknown', '缺少原进程句柄；先 reconcile，不自动重试')
        try:
            await asyncio.shield(asyncio.get_running_loop().run_in_executor(None, entry['done'].wait))
        except asyncio.CancelledError:
            entry['cancel'].set()
            await drain(asyncio.get_running_loop().run_in_executor(None, entry['done'].wait))
            raise
        final = dict(entry['info'])
        if cancel.is_set() or not self.context.registry.is_enabled(spec.name) or final['status'] != 'completed':
            return ToolOutcome(spec.name, 'failed', '隔离分析未完成', result=json.dumps({
                'status': 'incomplete', 'retained_source': str(payload.frozen), 'process': final,
                'diagnostics': bytes(entry['output'].stderr).decode('utf-8', errors='replace')}),
                audit=final, redact_result_preview=True)
        try:
            raw = await thread_call(collect, service, final, limit=LIMITS['output_bytes'])
            result = await thread_call(publish, self.context.artifacts, payload, json.loads(raw))
        except (ValueError, KeyError) as exc:
            failed = ToolOutcome(spec.name, 'failed', str(exc), result=json.dumps({
                'status': 'incomplete', 'retained_source': str(payload.frozen)}), audit=final)
            service.store.operation_finish(final['operation_id'], 'not_committed', asdict(failed),
                                           {**entry['control'].facts, 'sheet_validation': 'failed'})
            return failed
        outcome = ToolOutcome(spec.name, 'executed' if result['status'] != 'incomplete' else 'failed',
                              result=json.dumps(result, ensure_ascii=False),
                              audit={**final, 'sheet_complete': True}, redact_result_preview=True)
        try:
            service.store.operation_finish(final['operation_id'], 'committed', asdict(outcome),
                                           {**entry['control'].facts, 'sheet': result})
        except Exception:
            return ToolOutcome(spec.name, 'unknown', '产物已发布但回执未确认；先 reconcile', result=outcome.result)
        return outcome
