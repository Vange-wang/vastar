"""T25 工具适配；不允许模型接受新任务，也不信任模型传入 Task 身份。"""
import json

from core.tools.operation_receipt import ToolOutcome
from core.tools.registry import ToolSpec


def install_task_tools(context, service):
    from core.harness.operations import OperationLedger
    context.task_service = service
    context.operation_ledger = OperationLedger(service.store)
    context.registry.register(ToolSpec(
        name="update_task_plan", description="T25：提议修订当前任务计划；不能改变验收标准或宣布完成。",
        parameters={"type": "object", "properties": {
            "expected_revision": {"type": "integer"}, "proposal": {"type": "object"}},
            "required": ["expected_revision", "proposal"], "additionalProperties": False},
        level="A", run=lambda **kwargs: "", effect="write", retryable=False))
    for name, description, props, required in [
        ("ask_task_question", "T24：询问缺失信息并暂停当前 Task；不是权限授权。",
         {"question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}},
          "required": {"type": "boolean"}, "expected_revision": {"type": "integer"},
          "timeout_s": {"type": "number"}}, ["question", "options", "required", "expected_revision"]),
        ("validate_task_step", "提交当前步骤证据给确定性验收器；Reviewer 只能建议。",
         {"step_id": {"type": "string"}, "evidence": {"type": "object"},
          "reviewer": {"type": "object"}}, ["step_id", "evidence"]),
    ]:
        context.registry.register(ToolSpec(name=name, description=description,
            parameters={"type": "object", "properties": props, "required": required, "additionalProperties": False},
            level="A", run=lambda **kwargs: "", effect="write", retryable=False))
    # 注册不等于启用；沿用 default-closed，用户 tools.enabled 显式启用。


def execute_task_tool(context, args, session_id, cancel_event):
    try:
        if cancel_event.is_set():
            return ToolOutcome("update_task_plan", "cancelled", reason="已取消")
        if context.task_service is None or set(args) - {"proposal", "expected_revision"}:
            raise ValueError("任务服务未装配或存在越界参数")
        task = context.task_service.propose(session_id, args["proposal"],
                                            expected_revision=args.get("expected_revision"))
        return ToolOutcome("update_task_plan", "executed", result=json.dumps(task, ensure_ascii=False),
                           audit={"task_id": task["task_id"], "plan_revision": task["current_plan_revision"]})
    except (ValueError, KeyError, TypeError) as exc:
        return ToolOutcome("update_task_plan", "rejected", reason=str(exc), origin="task_gate")


def execute_harness_tool(context, name, args, session_id, cancel_event):
    if name == "update_task_plan":
        return execute_task_tool(context, args, session_id, cancel_event)
    try:
        if cancel_event.is_set():
            return ToolOutcome(name, "cancelled", reason="已取消")
        if name == "ask_task_question":
            from core.harness.questions import QuestionService
            result = QuestionService(context.task_service).ask(session_id, **args)
        else:
            from core.harness.validators import Validator
            result = Validator(context.task_service.store, context.artifacts).validate_step(
                context.task_service, session_id, **args)
        return ToolOutcome(name, "executed", result=json.dumps(result, ensure_ascii=False),
                           audit={"task_result": result})
    except (ValueError, KeyError, TypeError) as exc:
        return ToolOutcome(name, "rejected", reason=str(exc), origin="task_gate")


def task_tool_guard(context, name, session_id):
    service = getattr(context, "task_service", None)
    task = service.store.task_for_session(session_id) if service else None
    if not task or name in {"update_task_plan", "ask_task_question", "validate_task_step"}:
        return None
    if task["status"] in {"WAITING_INPUT", "BLOCKED", "COMPLETED", "CANCELLED", "FAILED"}:
        return ToolOutcome(name, "rejected", reason=f"Task {task['status']}；请先处理任务状态", origin="task_gate")
    return None
