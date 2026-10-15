"""恢复顺序固定：owner → 水位/账本/引用 → 重建上下文；绝不重放工具。"""
import json
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

    def recover(self, task_id):
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
            with self.store._lock:
                if watermark(self.store, body["event_watermark"]["rowid"]) != body["event_watermark"]:
                    raise ValueError("事件水位/内容不匹配")
            trace.append("watermark")
            pending = self._operation_ids(task_id, pending=True)
            for operation_id in pending:
                report["operations"][operation_id] = self.ledger.reconcile(operation_id)
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
                raise ValueError("Run 预算尚未结算，恢复需核实，禁止重置")
            ref = body["context_ref"]
            _, content = self.artifacts.get(ref["artifact_id"], ref["version"])
            report.update(context=json.loads(content), task=current, budget_used=body["budget_used"],
                          budget_reserved=body["budget_reserved"], status=current["status"].lower())
            with self.store.transaction():
                self.store._conn.execute("UPDATE task_runtime SET recovery_error=NULL WHERE task_id=?", (task_id,))
            trace.append("context")
        except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
            report["reason"] = str(exc)
            self._needs_input(task_id, report["reason"])
        except Exception as exc:
            from core.artifacts import ArtifactError
            if not isinstance(exc, ArtifactError):
                raise
            report["reason"] = str(exc)
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
                                     " WHERE task_id=?", (self.store._now(), task_id))

    def recover_all(self):
        with self.store._lock:
            ids = [r[0] for r in self.store._conn.execute("SELECT DISTINCT task_id FROM checkpoints")]
        return [self.recover(task_id) for task_id in ids]

    def export(self, path):
        Path(path).write_text(encode(self.diagnostics), encoding="utf-8")
