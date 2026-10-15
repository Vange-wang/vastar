"""会话仅转接持久服务：排队、恢复和检查点判断在此层。"""
from dataclasses import fields

from core.events import EventSequencer
from core.harness.checkpoints import CheckpointService
from core.harness.recovery import RecoveryCoordinator


def install_recovery(context, coordinator):
    store = context.task_service.store
    coordinator.task_service = context.task_service
    coordinator.task_tools_context = context
    coordinator.checkpoints = CheckpointService(store, context.artifacts)
    coordinator.recovery = RecoveryCoordinator(store, context.artifacts)
    coordinator.recovery.recover_all()  # Store 已先持 owner，模型上下文尚未构建。
    from core.memory_tools import install_memory
    install_memory(context, coordinator)


def persist_pending(coordinator, item):
    if hasattr(coordinator, "ensure_session"):
        coordinator.ensure_session()  # submit 可先于 run_forever；FK 之前必须有会话。
    body = {f.name: getattr(item, f.name) for f in fields(item) if f.name != "sequencer"}
    coordinator._store.pending_save(coordinator.session_id, body)


def restore_pending(coordinator):
    from core.session import PendingInput
    loaded = getattr(coordinator, "_durable_loaded_sessions", set())
    if coordinator.session_id in loaded:
        return
    for body in coordinator._store.pending_list(coordinator.session_id):
        with coordinator._store._lock:
            seq = coordinator._store._conn.execute("SELECT COALESCE(MAX(seq),-1)+1 FROM events WHERE run_id=?",
                                                   (body["run_id"],)).fetchone()[0]
        coordinator._pending.append(PendingInput(**body, sequencer=EventSequencer(start=seq)))
    loaded.add(coordinator.session_id)
    coordinator._durable_loaded_sessions = loaded


def checkpoint_finished(coordinator, result):
    store = coordinator._store
    store.pending_state(result.run_id, "done")
    task = store.task_for_session(coordinator.session_id)
    if task is None or not hasattr(coordinator, "checkpoints"):
        return
    if result.durability != "confirmed":
        coordinator.recovery._needs_input(task["task_id"])
        return
    try:
        from core.harness.task_runtime import TaskRuntime
        runtime = TaskRuntime(coordinator.task_service)
        runtime.settle(task["task_id"], result)
        if runtime.snapshot(task["task_id"])["recovery_error"]:
            return  # 不得用降级后的新快照覆盖损坏证据，绕过恢复守卫。
        used, reserved = runtime.checkpoint_budget(task["task_id"])
        coordinator.checkpoints.save(task["task_id"], context={"task": task, "last_run_id": result.run_id},
                                      budget_used=used, budget_reserved=reserved)
        from core.budget_topup.runtime import notify_exhausted
        notify_exhausted(coordinator, result)
    except Exception as exc:
        coordinator.recovery._needs_input(task["task_id"])
        coordinator._renderer.render_note(f"检查点未提交，任务需核实：{exc}")
