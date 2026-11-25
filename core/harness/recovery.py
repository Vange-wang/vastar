"""恢复顺序固定：owner → 水位/账本/引用 → 重建上下文；绝不重放工具。"""
import json
import threading
import time
from contextlib import nullcontext
from pathlib import Path

from core.harness.checkpoints import digest, watermark
from core.harness.operations import OperationLedger
from core.tools.operation_resources import file_facts
from memory.repositories.tasks import encode


class RecoveryCoordinator:
    def __init__(self, store, artifacts):
        self.store, self.artifacts = store, artifacts
        self.ledger = OperationLedger(store)
        self.diagnostics = []

    def recover(self, task_id, *, delivery_run=None, write_guard=nullcontext):
        self.store._owner.assert_held()
        trace = ["owner"]
        report = {"task_id": task_id, "trace": trace, "operations": {}, "status": "needs_input"}
        try:
            row = self.store.checkpoint_latest(task_id)
            if row is None:
                raise ValueError("缺少已提交检查点；需要用户确认，不能从摘要猜补")
            body = json.loads(row["body"])
            if digest(body) != row["hash"]:
                raise ValueError("检查点 hash 损坏")
            wm = body["event_watermark"]
            if wm.get("algo") == "blocks:v1":
                from core.harness.watermark_blocks import watermark_blocks
                actual = watermark_blocks(self.store, wm["rowid"])
            else:
                actual = watermark(self.store, wm["rowid"])
            if actual != wm:
                raise ValueError("事件水位/内容不匹配")
            trace.append("watermark")
            from core.skills.code_change_delivery import unaccounted_operations
            with write_guard():
                candidates = self._operation_ids(task_id, pending=True)
            pending = unaccounted_operations(self.store, self.store.task_get(task_id), candidates)
            for operation_id in pending:
                from core.harness.recovery_delivery import recover_operation
                report["operations"][operation_id] = recover_operation(self, operation_id, write_guard=write_guard)
            trace.append("ledger")
            if any(r["status"] == "unknown" for r in report["operations"].values()):
                raise ValueError("操作结果 unknown；禁止重试外部提交")
            self._verify_refs(body)
            trace.append("references")
            current = self.store.task_get(task_id)
            if current["current_plan_revision"] != body["plan_revision"]:
                raise ValueError("计划版本已变化；须重新生成安全检查点")
            self._verify_task(current, body["task"])
            budget_row = self.store._conn.execute("SELECT active_run FROM task_runtime WHERE task_id=?", (task_id,)).fetchone()
            if budget_row and budget_row[0]:
                from core.harness.recovery_delivery import permit_retained_budget
                permit_retained_budget(self.store, current, body, budget_row[0], delivery_run)
            ref = body["context_ref"]
            _, content = self.artifacts.get(ref["artifact_id"], ref["version"])
            report.update(context=json.loads(content), task=current, budget_used=body["budget_used"],
                          budget_reserved=body["budget_reserved"], status=current["status"].lower())
            with write_guard():
                if budget_row:
                    from core.harness.task_runtime import TaskRuntime
                    from core.harness.task_service import TaskService
                    used, reserved = TaskRuntime(TaskService(self.store, '')).checkpoint_budget(task_id)
                    report.update(budget_used=used, budget_reserved=reserved)
                with self.store.transaction():
                    self.store._conn.execute("UPDATE task_runtime SET recovery_error=NULL WHERE task_id=?", (task_id,))
            trace.append("context")
        except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
            report["reason"] = str(exc)
            with write_guard():
                self._needs_input(task_id, report["reason"])
        except Exception as exc:
            from core.artifacts import ArtifactError
            if not isinstance(exc, ArtifactError):
                raise
            report["reason"] = str(exc)
            with write_guard():
                self._needs_input(task_id, report["reason"])
        report["completed_operations"] = self._operation_ids(task_id, pending=False)
        self.diagnostics.append(report)
        return report

    def _verify_task(self, current, snapshot):
        for key in ("goal", "source_ref", "constraints", "acceptance"):
            if current[key] != snapshot[key]:
                raise ValueError(f"任务事实 {key} 与检查点不匹配；禁止猜补")
        def facts(task):
            return [(s["step_id"], s["goal"], s["depends_on"], s.get("acceptance_ids", []), s.get("independent", False)) for s in task["steps"]]
        if facts(current) != facts(snapshot):
            raise ValueError("步骤事实与检查点不匹配")

    def _verify_refs(self, body):
        for expected in body.get("validation_records", []):
            actual = self.store._conn.execute("SELECT * FROM task_validations WHERE validation_id=?", (expected["validation_id"],)).fetchone()
            if actual is None or dict(actual) != expected:
                raise ValueError("验收证据记录缺失或损坏")
        memory = self.store._conn.execute("SELECT COALESCE(MAX(revision),0) FROM memory_revisions").fetchone()[0]
        if memory < body["memory_revision"]:
            raise ValueError("记忆版本回退或缺失")
        ref = body["context_ref"]
        meta, _ = self.artifacts.get(ref["artifact_id"], ref["version"])
        if meta["hash"] != ref["hash"]:
            raise ValueError("上下文快照 hash 不匹配")
        for reference in body.get("artifact_refs", []):
            meta, _ = self.artifacts.get(reference["artifact_id"], reference["version"])
            if meta["hash"] != reference["hash"]:
                raise ValueError("步骤证据产物 hash 不匹配")
        snapshots = body["file_snapshots"]
        # 检查点之后已核实操作可能合法更新文件；以最新已提交事实为准。
        for operation_id in self._operation_ids(body["task_id"], pending=False):
            op = self.store.operation_get(operation_id)
            if body.get("operation_versions", {}).get(operation_id) == digest(op["facts"]):
                continue
            snapshots.update(op["facts"].get("after", op["facts"].get("expected", {})))
        if file_facts([Path(p) for p in snapshots]) != snapshots:
            raise ValueError("文件内容 hash/快照不匹配；需要核实外部变化")

    def _operation_ids(self, task_id, *, pending):
        clause = "state IN ('prepared','executing','unknown')" if pending else "state='committed'"
        with self.store._lock:
            return [r[0] for r in self.store._conn.execute(
                "SELECT operation_id FROM tool_operations WHERE task_id=? AND " + clause + " ORDER BY rowid",
                (task_id,))]

    def _needs_input(self, task_id, reason="检查点或持久化需核实"):
        with self.store.transaction():
            self.store._conn.execute("INSERT OR IGNORE INTO task_runtime(task_id) VALUES(?)", (task_id,))
            self.store._conn.execute("UPDATE task_runtime SET recovery_error=? WHERE task_id=?", (reason, task_id))
            self.store._conn.execute("UPDATE tasks SET status='WAITING_INPUT',revision=revision+1,updated_at=?"
                                     " WHERE task_id=? AND status NOT IN ('COMPLETED','CANCELLED','BLOCKED')",
                                     (self.store._now(), task_id))

    def recover_all(self):
        with self.store._lock:
            ids = [r[0] for r in self.store._conn.execute("SELECT DISTINCT task_id FROM checkpoints")]
        return [self.recover(task_id) for task_id in ids]

    def sweep_interrupted(self):
        """启动崩溃清扫（纯 SQL，毫秒级）：active_run 属死 owner 且有检查点的任务标 WAITING_INPUT（KM-27）。

        精确复刻旧 recover_all 对这类任务的结局（permit_retained_budget 抛错→needs_input），
        不做水位哈希；核验留给 verify_all_background / /task resume。
        """
        from core.harness.recovery_delivery import abandoned_run
        with self.store._lock:
            rows = self.store._conn.execute(
                "SELECT task_id, active_run FROM task_runtime"
                " WHERE active_run IS NOT NULL AND active_run != ''"
                " AND task_id IN (SELECT DISTINCT task_id FROM checkpoints)").fetchall()
            run_ids = [r["active_run"] for r in rows]
            runs = {}
            if run_ids:
                placeholders = ",".join("?" * len(run_ids))
                runs = {r["run_id"]: dict(r) for r in self.store._conn.execute(
                    "SELECT * FROM runs WHERE run_id IN (" + placeholders + ")", run_ids)}
        marked = []
        for row in rows:
            run = runs.get(row["active_run"])
            if run is not None and abandoned_run(self.store, run):
                self._needs_input(row["task_id"], "进程中断，恢复核验延后到续跑")
                marked.append(row["task_id"])
        return marked

    def verify_all_background(self, *, start_delay_s=0.5, idle_s=0.05, thread_factory=threading.Thread):
        """后台逐任务核验（KM-27）：不阻塞启动；每任务一次水位哈希，任务间让出。

        跳过 active_run 属当前 owner 的活动任务（其一致性由续跑/检查点路径守护）；
        结论与 recover() 一致：失败标 needs_input，成功清 recovery_error。
        """
        if getattr(self, "_verify_thread", None) is not None:
            return self._verify_thread
        with self.store._lock:
            checkpoint_limit = self.store._conn.execute("SELECT COALESCE(MAX(rowid),0) FROM checkpoints").fetchone()[0]

        def work():
            from core.harness.recovery_delivery import (
                BackgroundVerification,
                VerificationSuperseded,
            )
            if start_delay_s:
                time.sleep(start_delay_s)
            try:
                from core.harness.watermark_blocks import ensure_blocks
                ensure_blocks(self.store, budget_s=1.0)  # TASK-174：派生块表时间盒回填（游标自导出）
            except Exception as exc:
                self.diagnostics.append({"task_id": None, "status": "wm_blocks_backfill", "reason": str(exc)})
            try:
                with self.store._lock:
                    ids = [r[0] for r in self.store._conn.execute(
                        "SELECT task_id, MAX(created_at) AS m FROM checkpoints WHERE rowid<=?"
                        " GROUP BY task_id ORDER BY m DESC", (checkpoint_limit,))]
                for task_id in ids:
                    try:
                        guard = BackgroundVerification(self.store, task_id, checkpoint_limit)
                        self.recover(task_id, write_guard=guard.write)
                    except VerificationSuperseded:
                        continue
                    except Exception as exc:  # recover 自身已分级；此处只兜线程不死
                        self.diagnostics.append({"task_id": task_id, "status": "verify_error", "reason": str(exc)})
                    if idle_s:
                        time.sleep(idle_s)
            except Exception as exc:
                self.diagnostics.append({"task_id": None, "status": "verify_aborted", "reason": str(exc)})

        self._verify_thread = thread_factory(target=work, name="recovery-verify", daemon=True)
        self._verify_thread.start()
        return self._verify_thread

    def export(self, path):
        Path(path).write_text(encode(self.diagnostics), encoding="utf-8")
