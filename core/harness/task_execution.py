"""会话适配：任务上下文、预算、状态一起生效；没有 Task 时行为不变。"""
import hashlib
import json
import time
from dataclasses import replace
from pathlib import Path

from core.events import EventFactory, EventType
from core.run_state import RunResult

PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "task_execution_gpt.md"


async def run_with_task(coordinator, request):
    if not request.task_id or request.cancel_event.is_set():
        return await coordinator._runtime.run(request, coordinator._emit_checked)
    service = coordinator.task_service
    try:
        # 原始输入仍独立存档；一次输入只澄清当前 Task，不能自动创建目标。
        await coordinator._writer.flush()
        task = service.current(request.session_id)
        service.clarify(request.session_id, request.input_message_id, expected_revision=task["revision"])
        from core.prompt import strip_comments
        text = strip_comments(PROMPT_PATH.read_text(encoding="utf-8"))
        from core.budget_topup.runtime import reserve_task
        step, calls, tokens = reserve_task(coordinator, request)
        request.step_id = step
        task = service.current(request.session_id)
        old_calls, old_tokens = request.token_cfg.get("run_max_calls"), request.token_cfg.get("run_max_estimated_tokens")
        request.token_cfg = {**request.token_cfg, "run_max_calls": min(calls, old_calls) if old_calls is not None else calls,
            "run_max_estimated_tokens": min(v for v in (tokens, old_tokens) if v is not None)
            if tokens is not None or old_tokens is not None else None}
        from core.harness.questions import QuestionService
        questions = QuestionService(service)
        task["questions"] = [questions.get(r[0]) for r in service.store._conn.execute(
            "SELECT question_id FROM task_questions WHERE task_id=?", (task["task_id"],))]
        digest = hashlib.sha256(text.encode()).hexdigest()
        request.prompt_bundle = replace(request.prompt_bundle,
            version=request.prompt_bundle.version + "+task-v1.0",
            task_text=text + "\n任务事实：\n" + json.dumps(task, ensure_ascii=False),
            files={**request.prompt_bundle.files, "task_execution_gpt.md": digest})
    except (ValueError, OSError) as exc:
        result = RunResult(request.run_id, status="incomplete", finish_reason="needs_input")
        result.warnings = [str(exc)]
        factory = EventFactory(request.session_id, request.run_id, 0, sequencer=request.event_sequencer)
        await coordinator._emit_checked(factory.build(EventType.RUN_ENDED, {
            "status": "incomplete", "finish_reason": "needs_input", "warnings": result.warnings}))
        from core.budget_topup.runtime import exhaustion_note
        coordinator._renderer.render_note(exhaustion_note(coordinator, refusal_note(
            coordinator._store, request.session_id, str(exc))))
        return result
    from core.budget_topup.runtime import run_budgeted
    return await run_budgeted(coordinator, request)


def refusal_note(store, session_id, error: str) -> str:
    """Vastar runtime component."""
    task = store.task_for_session(session_id)
    if task is None:
        return error
    with store._lock:
        row = store._conn.execute("SELECT recovery_error FROM task_runtime WHERE task_id=?",
                                  (task["task_id"],)).fetchone()
    if row and row["recovery_error"]:
        if store.checkpoint_latest(task["task_id"]) is None:
            return (error + "；现场无检查点、不可恢复——/new 开新会话继续对话"
                    "（任务留在原会话，可 /task attach 回挂；/task resume 仅在有检查点时可用）")
        return error + "；先 /task resume 核查恢复后再输入（/task status 查看）"
    steps = [s for s in task["steps"] if s["status"] in {"RUNNING", "VERIFYING"}]
    if len(steps) != 1:
        return (error + "；先 /task step <Step ID> 选定一个可执行步骤再输入"
                "（/task status 查看步骤；不继续任务可 /new 开新会话）")
    return error + "；/task status 查看任务状态"


def continue_precheck(store, session_id) -> str | None:
    """Vastar runtime component."""
    task = store.task_for_session(session_id)
    if task is None:
        return None   # 非任务态：/continue 语义不变
    if task["status"] in {"COMPLETED", "BLOCKED", "CANCELLED"}:
        return f"任务已 {task['status']}，不能续写；/task status 查看"
    steps = [s for s in task["steps"] if s["status"] in {"RUNNING", "VERIFYING"}]
    if len(steps) != 1:
        return ("任务态续写要求唯一执行中的步骤；先 /task step <Step ID> 选定再输入"
                "（/task status 查看步骤状态）")
    step = steps[0]
    with store._lock:
        row = store._conn.execute(
            "SELECT recovery_error, active_run, used_calls FROM task_runtime WHERE task_id=?",
            (task["task_id"],)).fetchone()
        required_pending = store._conn.execute(
            "SELECT COUNT(*) FROM task_questions WHERE task_id=? AND state='pending'"
            " AND required=1 AND expires_at>?", (task["task_id"], time.time())).fetchone()[0]
        unsettled = store._conn.execute(
            "SELECT 1 FROM tool_operations WHERE task_id=?"
            " AND state IN ('prepared','executing','unknown')", (task["task_id"],)).fetchone()
    if row and row["recovery_error"]:
        return f"任务需先核实恢复（{row['recovery_error']}）；/task resume 恢复后再续写"
    if required_pending and not step["independent"]:
        return "有必答问题未回答；/task answer 回答后再续写"
    if unsettled:
        return "有未落定动作；先 /task reconcile <Operation ID> 核实"
    if row and row["active_run"]:
        return "存在未结算的 Run 预算，必须核实；/task reconcile 或 /task resume"
    from core.budget_topup.single import effective_budget
    limit = effective_budget(store, task).get("max_calls")
    if type(limit) is not int or limit <= 0:
        return "任务缺少明确调用预算，无法续写；/task status 查看"
    if row and limit - row["used_calls"] <= 0:
        return "Task 调用预算已耗尽；/task budget 查看成果、未完成项与逐次授权续加入口"
    from core.budget_topup.single import exhausted_reason
    return exhausted_reason(store, task)


def annotate_task_outcome(store, event):
    if event.type == EventType.RUN_ENDED:
        task = store.task_for_session(event.session_id)
        if task:
            event.payload.update(task_id=task["task_id"], task_status=task["status"],
                                 remaining_steps=task["remaining_work"])
