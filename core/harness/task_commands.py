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
        "/task attach <Task ID>；/task step <Step ID>；/task run [Step ID] 执行已确认步骤；/task resume；/task budget [single|worker]；"
        "/task reconcile <Operation ID>；/task validate <JSON: step_id,evidence>；/task deliver [验收键到operation_id的JSON] 本轮验收交付；"
        "/task outputs 复查已验收文件；/task answer <JSON: question_id,answer>；/task cancel-question <ID>；/task tools enable。"
        "明确的自然语言操作目标也可起草；/task ok 前不执行工具。"
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
    if cmd in {"new", "ok", "run"}:
        from core.llm.pricing import require_task_model
        require_task_model(coord)
    service, session = coord.task_service, coord.session_id
    if cmd in {"", "help"}:
        return HELP
    if cmd == "accept":
        as_json = tail.startswith("--json")
        data = json.loads(tail[6:].strip() if as_json else tail)
        require_max_calls(data.get("budget"))   # HM-146：accept 期即校验（给示例）
        blocker = artifacts_blocker(coord)   # HM-149：产物库不可用在落库前拦（B13 口径不变）
        if blocker:
            raise ValueError(blocker)
        task = service.accept(session, _source(coord, arg), **data)
        return present_result(checkpoint_command(coord, task), as_json=as_json)   # HM-145
    if cmd == "new":
        return await task_new_command(coord, tail)   # HM-144：模型起草（走正常 Run）
    if cmd == "ok":
        return task_ok_command(coord, tail)          # 用户显式确认 → 复用 accept 落库
    if cmd == "drop":
        return task_drop_command(coord)
    if cmd == "deliver":
        from core.harness.task_delivery import deliver_task
        return deliver_task(coord, tail)
    if cmd == "outputs":
        from core.harness.task_delivery import task_outputs
        return task_outputs(coord)
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
    if cmd == "run":
        return await run_accepted_step(coord, task, tail.strip())
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


async def run_accepted_step(coord, task, requested):
    """Queue a previously accepted step; leave any in-flight or attempted step to recovery."""
    if coord.has_active_or_pending():
        raise ValueError('已有输入正在执行或排队，请等待当前回执')
    steps = task['steps']
    done = {s['step_id'] for s in steps if s['status'] == 'COMPLETED'}
    active = next((s for s in steps if s['status'] in {'RUNNING', 'VERIFYING'}), None)
    if active and requested and requested != active['step_id']:
        raise ValueError('当前步骤尚待验收，请先核对 /task status')
    chosen = active or next((s for s in steps if (not requested or s['step_id'] == requested)
                             and s['status'] == 'READY' and done.issuperset(s['depends_on'])), None)
    if not chosen:
        raise ValueError('没有符合依赖条件的待执行步骤')
    sid = chosen['step_id']
    attempted = coord.task_service.store._conn.execute(
        'SELECT 1 FROM runs WHERE task_id=? AND step_id=? LIMIT 1', (task['task_id'], sid)).fetchone()
    if attempted:
        raise ValueError('当前步骤已有运行记录，请先 /task deliver 验收或 /task resume 核对恢复')
    selected = checkpoint_command(coord, coord.task_service.select_step(coord.session_id, sid))
    if selected.get('checkpoint_status'):
        return selected
    receipt = await coord.submit('执行已确认的任务步骤 ' + sid + '：' + chosen['goal']
                                 + '\n遵守已确认的约束及工具审批，按原验收条件交付。')
    return {'step_id': sid, 'input_id': receipt.input_id, 'state': receipt.state}


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
