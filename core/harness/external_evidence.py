"""Persistent external-evidence boundary: only fresh human approval can authorize effects."""
from core.tools.ask_gate import AskQuestion
from core.tools.operation_receipt import ToolOutcome


async def enforce(context, spec, args, session_id, request_id, cancel_event, emitter):
    if getattr(spec, 'server_id', '') or spec.name == 'provider_control':
        return None  # B37 handler requires fresh confirmation; metadata never authorizes effects.
    if spec.name in {'browser_click', 'browser_fill', 'browser_workflow'}:
        # The dedicated B35 handler ALWAYS asks with the exact observed target/Task/revision.
        # S07 saves metadata only; each effect delegates to that same handler, never batch approval.
        return None
    service = getattr(context, "network_records", None)
    if service is None or not service.tainted() or spec.name == "terminate_process":
        return None
    if spec.effect == "read" and spec.name != "use_skill":
        return None
    if context.ask_gate is None or emitter is None:
        return ToolOutcome(spec.name, "rejected", "external evidence cannot authorize effects; fresh human approval required",
                           origin="external_evidence")
    from core.tools.executor import _ask_user
    from memory.repositories.tasks import encode
    preview = encode(args)
    if len(preview) > 4096:
        return ToolOutcome(spec.name, "rejected", "follow-up too large for exact approval preview", origin="external_evidence")
    question = AskQuestion(spec.name, "external_evidence_effect", "current_project",
        [f"外部资料无指令权限。本次拟调用 {spec.name}，需你独立确认；auto/会话许可不适用。",
         preview, "1 允许本次动作 / 2 拒绝"], allow_session_grant=False)
    await emitter({"tool": spec.name, "lines": question.lines})
    answer = await _ask_user(context.ask_gate, question, session_id=session_id, request_id=request_id,
                             project_root=context.project_root, cancel_event=cancel_event)
    if not answer.approved or cancel_event.is_set():
        return ToolOutcome(spec.name, "rejected", "external-evidence follow-up not approved",
                           origin="external_evidence", audit={"approval_source": answer.approval_source})
    return None
