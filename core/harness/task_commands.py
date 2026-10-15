"""结构化任务命令；UI 不承担任务事实判定。"""
import json
import uuid

from core.harness.questions import QuestionService
from core.harness.task_draft import (
    artifacts_blocker,
    present_result,
    require_max_calls,
    task_drop_command,
    task_new_command,
    task_ok_command,
)
from core.harness.task_runtime import TaskRuntime
from core.harness.validators import Validator

HELP = ("/task new <自然语言目标> 模型起草计划草案；/task ok [--json] 确认草案并落库；"
        "/task drop 放弃草案；/task accept [--json] <JSON: goal,acceptance,constraints,budget,steps>"
        "（budget 须含 \"max_calls\": 正整数；默认人话摘要，--json 输出明细）；/task status；"
        "/task attach <Task ID>；/task step <Step ID>；/task resume；/task budget [single|worker]；"
        "/task reconcile <Operation ID>；/task validate <JSON: step_id,evidence>；"
        "/task answer <JSON: question_id,answer>；/task cancel-question <ID>；/task tools enable。"
        "accept/new+ok/attach 显式关联任务；step 选定后普通输入执行；旧会话不自动变成任务。")


def _source(coord, text):
    coord.ensure_session()
    message_id = "task-input-" + uuid.uuid4().hex
    coord._store.append_message(message_id, coord.session_id, role="user", content=text, origin="user")
    return message_id


async def task_command(coord, arg):
    if coord.is_busy():
        raise ValueError("当前 Run 尚未结束；先取消或等结束再操作 Task")
    if not hasattr(coord, "task_service"):
        raise ValueError("任务服务未装配")
    cmd, _, tail = arg.partition(" ")
    service, session = coord.task_service, coord.session_id
    if cmd in {"", "help"}:
        return HELP
    if cmd == "accept":
        as_json = tail.startswith("--json")
        data = json.loads(tail[6:].strip() if as_json else tail)
        require_max_calls(data.get("budget"))   
        blocker = artifacts_blocker(coord)   
        if blocker:
            raise ValueError(blocker)
        task = service.accept(session, _source(coord, arg), **data)
        return present_result(checkpoint_command(coord, task), as_json=as_json)   
    if cmd == "new":
        return await task_new_command(coord, tail)   
    if cmd == "ok":
        return task_ok_command(coord, tail)          # 用户显式确认 → 复用 accept 落库
    if cmd == "drop":
        return task_drop_command(coord)
    if cmd == "attach":
        coord.ensure_session()
        return service.attach(session, tail.strip())
    if cmd == "budget":
        from core.budget_topup.commands import budget_command
        result = budget_command(coord, tail, lambda: _source(coord, '/task ' + arg))
        return checkpoint_command(coord, result) if tail.startswith('approve ') else result
    task = service.current(session)
    questions = QuestionService(service)
    if cmd == "status":
        return {"task": task, "questions": questions.pending(task["task_id"]),
                "budget": TaskRuntime(service).snapshot(task["task_id"])}
    if cmd == "step":
        return checkpoint_command(coord, service.select_step(session, tail.strip()))
    if cmd == "validate":
        result = Validator(service.store, coord.task_tools_context.artifacts).validate_step(service, session, **json.loads(tail))
        return checkpoint_command(coord, result)
    if cmd == "answer":
        data = json.loads(tail)
        result = questions.answer(session, data["question_id"], data["answer"], message_id=_source(coord, arg))
        return checkpoint_command(coord, result)
    if cmd == "cancel-question":
        return questions.cancel(session, tail.strip())
    if cmd == "reconcile":
        op = service.store.operation_get(tail.strip())
        if not op or op["task_id"] != task["task_id"]:
            raise ValueError("Operation 不属于当前 Task")
        return coord.recovery.ledger.reconcile(tail.strip())
    if cmd == "resume":
        report = coord.recovery.recover(task["task_id"])
        if report["status"] not in {"ready", "running", "verifying"}:
            return report
        await coord.submit("继续当前 Task 的已选步骤；保留原验收、计划版本与预算。")
        return {"resume": "queued", "recovery": report}
    if cmd == "tools" and tail.strip() == "enable":
        registry = coord.task_tools_context.registry
        for name in ("update_task_plan", "ask_task_question", "validate_task_step"):
            if name not in registry.enabled:
                registry.enabled.append(name)
        _source(coord, arg)
        return "已按当前用户命令启用三项任务工具；权限 ASK 和文件工具开关不变。"
    raise ValueError(HELP)


def checkpoint_command(coord, result):
    """命令完成也是安全边界；不等下一条模型 Run 才保存已接受的任务/验收事实。"""
    from core.artifacts import ArtifactError
    task = coord.task_service.current(coord.session_id)
    runtime = TaskRuntime(coord.task_service)
    if runtime.snapshot(task["task_id"])["recovery_error"]:
        return result
    try:
        used, reserved = runtime.checkpoint_budget(task["task_id"])
        coord.checkpoints.save(task["task_id"], context={"task": task, "command_result": result},
                               budget_used=used, budget_reserved=reserved)
    except (ValueError, OSError, ArtifactError) as exc:
        coord.recovery._needs_input(task["task_id"], str(exc))
        return {"result": result, "checkpoint_status": "needs_input", "reason": str(exc)}
    return result
