"""Specialized tool handlers share registry/Task/Skill gates without growing execute."""
from core.harness.task_tools import execute_harness_tool, task_tool_guard
from core.skills.workflow_guard import reject_write
from core.tools.analysis_dispatch import dispatch as analysis_dispatch
from core.tools.analysis_dispatch import guard as report_guard


async def dispatch(context, spec, args, *, args_summary, session_id, request_id,
                   operation_id, cancel_event, ask_emitter):
    name = spec.name
    from core.tools.document_tools import NAMES
    from core.tools.document_tools import guard as document_guard
    denied = document_guard(context, spec)
    if denied is not None:
        return denied
    denied = report_guard(context, spec)
    if denied is not None:
        return denied
    from core.skills.research_tools import guard
    denied = guard(context, spec, session_id, request_id)
    if denied is not None:
        return denied
    from core.harness.external_evidence import enforce
    denied = await enforce(context, spec, args, session_id, request_id, cancel_event, ask_emitter)
    if denied is not None:
        return denied
    # Inspection/collection remains available after a Task becomes terminal.
    if name not in {"read_process", "terminate_process"}:
        denied = task_tool_guard(context, name, session_id)
        if denied is not None:
            return denied
    if name in {"update_task_plan", "ask_task_question", "validate_task_step"}:
        return execute_harness_tool(context, name, args, session_id, cancel_event)
    denied = reject_write(context.skills, spec, args_summary)
    if denied is not None:
        return denied
    if name in {"web_search", "read_web_page"}:
        return await context.network_service.execute(spec, args, session_id, request_id, operation_id, cancel_event)
    if name in NAMES:
        return await context.document_service.execute(spec, args, session_id, request_id,
                                                       operation_id, cancel_event, ask_emitter)
    if name in {"report_capabilities", "generate_report"}:
        return await context.report_service.execute(spec, args, session_id, request_id,
                                                     operation_id, cancel_event, ask_emitter)
    if name == "verify_research":
        from core.skills.research_tools import execute
        return await execute(context, args, session_id, request_id, cancel_event)
    if name == "write_memory":
        from core.memory_tools import execute_memory_tool
        return execute_memory_tool(context, args, session_id, request_id, operation_id, cancel_event)
    if name == "forget_memory":
        from core.memory_forget_tools import execute_forgetting
        return execute_forgetting(context, args, session_id, request_id, operation_id, cancel_event)
    if name in {"start_process", "read_process", "terminate_process"}:
        from core.tools.process_tools import execute_process_tool
        return await execute_process_tool(context, spec, args, session_id=session_id,
                                          request_id=request_id, operation_id=operation_id,
                                          cancel_event=cancel_event, ask_emitter=ask_emitter)
    return await analysis_dispatch(context, spec, args, session_id, request_id,
                                   operation_id, cancel_event, ask_emitter)
