"""T11/T12/T13: separate enable flags and per-command approval, with trusted Task scope."""
import asyncio
import threading

from core.tools.ask_gate import AskQuestion
from core.tools.operation_receipt import ToolOutcome
from core.tools.operation_runner import drain
from core.tools.process_service import ProcessService
from core.tools.registry import ToolSpec
from memory.repositories.tasks import encode


class CommandCancellation:
    def __init__(self, event, authorized=lambda: True):
        self.event, self.token, self.authorized = event, threading.Event(), authorized

    def is_set(self):
        return self.token.is_set() or self.event.is_set() or not self.authorized()

    def set(self):
        self.token.set()


def install_process_tools(context):
    context.process_service = ProcessService(context.execution_sandbox, context.operation_ledger, context.artifacts)
    context.process_service.registry = context.registry
    context.execution_sandbox.process_service = context.process_service
    identity = {"job_id": {"type": "string"}, "started_at": {"type": "string"}}
    schemas = {
        "start_process": ({"script": {"type": "string", "maxLength": 8192},
                           "encoding": {"enum": ["utf-8", "gbk"]},
                           "idle_timeout": {"type": "number", "minimum": 0.01, "maximum": 86400}}, ["script"]),
        "read_process": ({**identity, "cursor": {"type": "integer", "minimum": 0},
                          "max_bytes": {"type": "integer", "minimum": 1, "maximum": 32768}}, list(identity)),
        "terminate_process": (identity, list(identity))}
    for name, (properties, required) in schemas.items():
        context.registry.register(ToolSpec(
            name, {"start_process": "批准后启动只读离线 pwsh；Task 必须已选 Step。启动不表示完成。idle_timeout 为无输出秒数。",
                   "read_process": "按 job_id/启动时间增量读 stdout/stderr；字节游标和 artifact 保留原文。",
                   "terminate_process": "按当前 Task 所有权停止完整 Job 进程树；不回滚已发命令。"}[name],
            {"type": "object", "properties": properties, "required": required, "additionalProperties": False},
            "D" if name == "start_process" else "A", lambda **_: "special handler required",
            effect="read" if name == "read_process" else "execute", resource_scope="process",
            retryable=False, cooperative=True))


async def execute_process_tool(context, spec, args, *, session_id, request_id,
                               operation_id, cancel_event, ask_emitter):
    try:
        service = context.process_service
        task = context.task_service.current(session_id)
        if set(args) - set(spec.parameters["properties"]) or any(k not in args for k in spec.parameters["required"]):
            raise ValueError("invalid process tool parameters")
        if spec.name == "start_process":
            outcome = await start_approved(context, spec, args, task["task_id"], session_id,
                                            request_id, operation_id, cancel_event, ask_emitter)
        else:
            kwargs = {**args, "owner": task["task_id"]}
            if spec.name == "read_process":
                size = args.get("max_bytes", 16384)
                if type(size) is not int or not 1 <= size <= 32768:
                    raise ValueError("max_bytes must be in [1,32768]")
                kwargs["max_bytes"] = min(size, 16384)  # leave room for base64, previews and metadata
            function = service.read if spec.name == "read_process" else service.terminate
            result = await drain(asyncio.get_running_loop().run_in_executor(None, lambda: function(**kwargs)))
            outcome = ToolOutcome(spec.name, "executed", result=encode(result),
                                  audit={"job_id": args["job_id"], "owner": task["task_id"]})
    except (ValueError, PermissionError, RuntimeError) as exc:
        outcome = ToolOutcome(spec.name, "rejected", str(exc), origin="process_gate")
    except Exception as exc:
        outcome = ToolOutcome(spec.name, "unknown", f"process service failed: {type(exc).__name__}", origin="process_service")
    context.registry.record_call(spec.name, outcome.status, outcome.duration_ms)
    return outcome


async def start_approved(context, spec, args, owner, session_id, request_id, operation_id, cancel_event, emitter,
                         *, payload=None):
    from core.tools.executor import _ask_user

    service = context.process_service
    service.require_task(owner, request_id)
    if not context.execution_sandbox.preflight()["execution_effective"]:
        raise PermissionError("D disabled or sandbox unavailable; no command issued")
    question = AskQuestion(spec.name, "execute", "private_readonly_offline",
                           ["执行仅限只读离线 AppContainer，cwd 是私有目录；非项目 shell。",
                            args["script"], *(payload.approval_lines if payload else []),
                            "1 允许 / 2 拒绝（仅本条命令，auto 不免审批）"],
                           allow_session_grant=False)
    if emitter:
        await emitter({"tool": spec.name, "lines": question.lines, "session_id": session_id, "request_id": request_id})
    answer = await _ask_user(context.ask_gate, question, session_id=session_id, request_id=request_id,
                             project_root=context.project_root, cancel_event=cancel_event)
    if not answer.approved or cancel_event.is_set():
        return ToolOutcome(spec.name, "rejected", "command not approved", origin="ask_gate",
                           audit={"approval_source": answer.approval_source})
    epoch = context.ask_gate.authorization_epoch
    token = CommandCancellation(cancel_event, lambda: context.registry.is_enabled(spec.name)
                                and context.ask_gate.authorization_epoch == epoch
                                and context.execution_sandbox.preflight()["execution_effective"])
    worker = asyncio.get_running_loop().run_in_executor(None, lambda: service.start(
        **args, task_id=owner, run_id=request_id, spec=spec, operation_id=operation_id, approved=True,
        cancel=token, **({"payload": payload} if payload is not None else {})))
    cancelled = asyncio.create_task(cancel_event.wait())
    try:
        done, _ = await asyncio.wait({worker, cancelled}, return_when=asyncio.FIRST_COMPLETED)
        if cancelled in done:
            token.set()
        result = await drain(worker)
        if token.is_set():
            await drain(asyncio.get_running_loop().run_in_executor(None, lambda: _collect_cancelled(service, result)))
        return result
    except asyncio.CancelledError:
        token.set()
        result = await drain(worker)
        await drain(asyncio.get_running_loop().run_in_executor(None, lambda: _collect_cancelled(service, result)))
        raise
    finally:
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)


def _collect_cancelled(service, result):
    info = result.audit
    if info.get("job_id") in service.entries:
        service.terminate(info["job_id"], owner=info["owner"], started_at=info["started_at"], reason="cancelled")
