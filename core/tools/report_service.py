"""S03 approval, isolated rendering, failure source retention and receipt reuse."""
import asyncio
import json
import uuid
from dataclasses import asdict

from core.tools.document_results import collect
from core.tools.operation_receipt import ToolOutcome
from core.tools.operation_runner import drain, thread_call
from core.tools.permissions import resolve_read_path
from core.tools.process_tools import start_approved
from core.tools.report_adapter import ReportPayload, ReportTableError, resources
from core.tools.report_results import publish


def _incomplete(outcome, staging, **details):
    saved = None
    if staging is not None:
        source = staging / "source.json"
        try:
            if source.is_file():
                saved = str(source.absolute())
        except OSError as exc:
            details["source_check_error"] = type(exc).__name__
    if saved:
        outcome.reason += f" 可编辑源：{saved}"
    outcome.result = json.dumps({"status": "incomplete", "editable_source": saved, **details},
                                ensure_ascii=False)
    return outcome


class ReportService:
    def __init__(self, context):
        self.context = context

    async def execute(self, spec, args, session_id, run_id, operation_id, cancel, emitter):
        staging = None
        try:
            if spec.name == "report_capabilities":
                return ToolOutcome(spec.name, "executed", result=json.dumps({"versions": resources(),
                    "formats": ["pdf", "docx"], "images": "bounded PNG; explicit report permission required",
                    "isolation": self.context.execution_sandbox.preflight()}, ensure_ascii=False))
            if (set(args) - {'path', 'table'} or not isinstance(args.get('path'), str)
                    or ('table' in args and not isinstance(args['table'], dict))):
                raise ReportTableError('需要报告源 JSON 路径，可选 table 指定原始记录数组的列')
            path = resolve_read_path(self.context.cwd or self.context.project_root, args["path"])
            staging = self.context.artifacts._require_dir().parent / "report-staging" / uuid.uuid4().hex
            payload = await thread_call(ReportPayload, path, staging, args.get('table'))
            owner = self.context.task_service.current(session_id)["task_id"]
            outcome = await start_approved(self.context, spec, {"script": payload.script}, owner, session_id,
                                           run_id, operation_id, cancel, emitter, payload=payload)
            if outcome.audit.get("report_complete"):
                return outcome
            if outcome.status != "executed":
                return _incomplete(outcome, staging, operation_result=outcome.result)
            return await self._finish(spec, payload, outcome.audit, cancel)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = f'未完成输出：{exc}。' if isinstance(exc, ReportTableError) else '未完成输出：源或受控渲染不可用。'
            failure = ToolOutcome(spec.name, "failed", reason,
                                  origin="report_gate", redact_result_preview=True)
            return _incomplete(failure, staging, diagnostics={"type": type(exc).__name__, "error": str(exc)})

    async def _finish(self, spec, payload, info, cancel):
        service = self.context.process_service
        entry = service.lookup(info["job_id"], info["owner"], info["started_at"])
        if entry.get("done") is None:
            unknown = ToolOutcome(spec.name, "unknown", "恢复操作无受控句柄；未完成输出，禁止自动重试")
            return _incomplete(unknown, payload.staging)
        try:
            await asyncio.shield(asyncio.get_running_loop().run_in_executor(None, entry["done"].wait))
        except asyncio.CancelledError:
            entry["cancel"].set()
            await drain(asyncio.get_running_loop().run_in_executor(None, entry["done"].wait))
            raise
        final = dict(entry["info"])
        if cancel.is_set() or not self.context.registry.is_enabled(spec.name) or final["status"] != "completed":
            raise ValueError("未完成输出：" + final["status"] + "; " +
                             bytes(getattr(entry["output"], "stderr", b"")).decode("utf-8", errors="replace"))
        raw = await thread_call(collect, service, final, limit=48 * 1024 * 1024)
        (payload.staging / "render-diagnostics.json").write_bytes(raw if len(raw) < 16384 else b'{}')
        try:
            result = await thread_call(publish, self.context.artifacts, payload, json.loads(raw))
        except (ValueError, KeyError) as exc:
            # Known validation failure: no delivery was published; preserve the source.
            failure = ToolOutcome(spec.name, "failed", "未完成输出：验证未通过。",
                                  audit=final, redact_result_preview=True)
            _incomplete(failure, payload.staging, diagnostics=str(exc))
            service.store.operation_finish(final["operation_id"], "not_committed", asdict(failure),
                                           {**entry["control"].facts, "report_validation": "failed before publication"})
            return failure
        outcome = ToolOutcome(spec.name, "executed", result=json.dumps(result, ensure_ascii=False),
                              audit={**final, "report_complete": True}, redact_result_preview=True)
        try:
            service.store.operation_finish(final["operation_id"], "committed", asdict(outcome),
                                           {**entry["control"].facts, "report": result})
        except Exception:
            return ToolOutcome(spec.name, "unknown", "输出已发布但回执未确认；先 reconcile，禁止自动重试",
                               result=outcome.result, audit=final, redact_result_preview=True)
        return outcome
