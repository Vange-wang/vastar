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
from core.tools.report_adapter import ReportPayload, resources
from core.tools.report_results import publish


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
            if set(args) != {"path"} or not isinstance(args["path"], str):
                raise ValueError("只接受报告源 JSON 路径")
            path = resolve_read_path(self.context.cwd or self.context.project_root, args["path"])
            staging = self.context.artifacts._require_dir().parent / "report-staging" / uuid.uuid4().hex
            payload = await thread_call(ReportPayload, path, staging)
            owner = self.context.task_service.current(session_id)["task_id"]
            outcome = await start_approved(self.context, spec, {"script": payload.script}, owner, session_id,
                                           run_id, operation_id, cancel, emitter, payload=payload)
            if outcome.audit.get("report_complete"):
                return outcome
            if outcome.status != "executed":
                outcome.result = json.dumps({"status": "incomplete", "editable_source": str(staging / "source.json"),
                                             "operation_result": outcome.result}, ensure_ascii=False)
                return outcome
            return await self._finish(spec, payload, outcome.audit, cancel)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return ToolOutcome(spec.name, "failed", "未完成输出：源或受控渲染不可用。", result=json.dumps({
                "status": "incomplete", "editable_source": str(staging / "source.json") if staging else None,
                "diagnostics": {"type": type(exc).__name__, "error": str(exc)}}, ensure_ascii=False),
                origin="report_gate", redact_result_preview=True)

    async def _finish(self, spec, payload, info, cancel):
        service = self.context.process_service
        entry = service.lookup(info["job_id"], info["owner"], info["started_at"])
        if entry.get("done") is None:
            return ToolOutcome(spec.name, "unknown", "恢复操作无受控句柄；未完成输出，禁止自动重试")
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
            failure = ToolOutcome(spec.name, "failed", "未完成输出：验证未通过。", result=json.dumps({
                "editable_source": str(payload.staging / "source.json"), "diagnostics": str(exc)}, ensure_ascii=False),
                audit=final, redact_result_preview=True)
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
