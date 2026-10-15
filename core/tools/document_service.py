"""Document read policy, explicit execution consent and B28 completion collection."""
import asyncio
import json
from dataclasses import asdict

from core.tools.document_adapter import DocumentPayload, capabilities
from core.tools.document_results import collect, publish
from core.tools.operation_receipt import ToolOutcome
from core.tools.operation_runner import drain, thread_call
from core.tools.permissions import resolve_read_path
from core.tools.process_tools import start_approved


class DocumentService:
    def __init__(self, context):
        self.context = context

    async def execute(self, spec, args, session_id, run_id, operation_id, cancel, emitter):
        context = self.context
        try:
            if spec.name == "document_capabilities":
                if args:
                    raise ValueError("capabilities accepts no parameters")
                return ToolOutcome(spec.name, "executed", result=json.dumps(capabilities(), ensure_ascii=False))
            if set(args) - {"path", "first", "count", "ocr"} or not isinstance(args.get("path"), str):
                raise ValueError("path required; unexpected document parameter")
            path = resolve_read_path(context.cwd or context.project_root, args["path"])
            kind = path.suffix.lower().lstrip(".")
            capability = capabilities().get(kind, capabilities()["unknown"])
            if capability["level"] not in {"full", "partial"}:
                return ToolOutcome(spec.name, "executed", result=json.dumps({"status": capability["level"],
                    "capability": capability, "records": [], "uncovered": [
                        "PDF 解析引擎不可用：未找到外部 pdftotext；未启动解析，也未退回非沙箱执行。"
                        if kind == "pdf" else "当前类型不支持提取；整份文档尚未识别。"],
                    "availability": {"status": "unavailable", "reason_code": "external_program_missing"
                                     if kind == "pdf" else "unsupported_type",
                                     "isolation": "not_started", "fallback_used": False},
                    "ocr": {"status": "unavailable", "confidence": None, "unrecognized_regions": ["unknown"]}}))
            payload = await thread_call(DocumentPayload, path, **{k: v for k, v in args.items() if k != "path"})
            owner = context.task_service.current(session_id)["task_id"]
            outcome = await start_approved(context, spec, {"script": payload.script}, owner, session_id,
                                            run_id, operation_id, cancel, emitter, payload=payload)
            if outcome.status != "executed" or outcome.audit.get("document_complete"):
                return outcome
            return await self._finish(spec, payload, outcome.audit, cancel)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return ToolOutcome(spec.name, "rejected", str(exc), origin="document_gate", redact_result_preview=True)

    async def _finish(self, spec, payload, info, cancel):
        service = self.context.process_service
        entry = service.lookup(info["job_id"], info["owner"], info["started_at"])
        if entry.get("done") is None:
            return ToolOutcome(spec.name, "unknown", "recovered job has no live owned handle; do not retry automatically")
        try:
            waiter = asyncio.get_running_loop().run_in_executor(None, entry["done"].wait)
            await asyncio.shield(waiter)
        except asyncio.CancelledError:
            entry["cancel"].set()
            await drain(asyncio.get_running_loop().run_in_executor(None, entry["done"].wait))
            raise
        final = dict(entry["info"])
        hashes = await thread_call(payload.verify_unchanged)
        if cancel.is_set() or final["status"] != "completed":
            return ToolOutcome(spec.name, "cancelled" if cancel.is_set() else "failed",
                "isolated extraction incomplete; inspect owned process receipt", audit={**final, "hashes": hashes})
        raw = await thread_call(collect, service, final)
        result = await thread_call(publish, self.context.artifacts, payload, json.loads(raw), final)
        outcome = ToolOutcome(spec.name, "executed", result=json.dumps(result, ensure_ascii=False),
                              audit={**final, "hashes": hashes, "document_complete": True}, redact_result_preview=True)
        facts = {**entry["control"].facts, "document_hashes": hashes, "document_artifact": result["artifact_ref"]}
        service.store.operation_finish(final["operation_id"], "committed", asdict(outcome), facts)
        return outcome
