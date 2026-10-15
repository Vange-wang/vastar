"""操作事实与步骤投影同事务；旧代次只能核实，不能续写旧执行回执。"""
import json

from memory.repositories.tasks import encode


class OperationRepository:
    def operation_get(self, operation_id):
        with self._lock:
            row = self._conn.execute("SELECT * FROM tool_operations WHERE operation_id=?",
                                     (operation_id,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            for key in ("resource_version", "approval_ref", "facts", "receipt", "reconcile_result"):
                result[key] = json.loads(result[key]) if result[key] is not None else None
            return result

    def operation_finish(self, operation_id, state, receipt, facts, *, reconciliation=None, fault=None):
        with self.transaction():
            operation = self.operation_get(operation_id)
            if operation is None:
                raise ValueError("缺失 prepared 操作意图")
            if reconciliation is None and operation["fencing_generation"] != self.fencing_generation:
                raise ValueError("过期 fencing_generation；拒绝旧执行者写回")
            ref = "receipt:" + operation_id if state == "committed" else None
            self._conn.execute("UPDATE tool_operations SET state=?,receipt_ref=?,receipt=?,facts=?,"
                               "reconcile_result=?,updated_at=? WHERE operation_id=?",
                               (state, ref, encode(receipt), encode(facts), encode(reconciliation),
                                self._now(), operation_id))
            if fault:
                fault("receipt_written")
            if operation["task_id"] and operation["step_id"]:
                self._operation_projection(operation, state, ref)

    def _operation_projection(self, operation, state, receipt_ref):
        row = self._conn.execute("SELECT evidence_refs,status FROM task_steps WHERE task_id=? AND step_id=?",
                                 (operation["task_id"], operation["step_id"])).fetchone()
        if row is None:
            raise ValueError("操作引用步骤已缺失；拒绝单边提交回执")
        refs = json.loads(row["evidence_refs"])
        if state == "committed" and receipt_ref not in refs:
            refs.append(receipt_ref)
        status = "VERIFYING" if state == "committed" else "BLOCKED"
        if row["status"] == "COMPLETED":
            status = "COMPLETED"
        self._conn.execute("UPDATE task_steps SET evidence_refs=?,status=?,revision=revision+1"
                           " WHERE task_id=? AND step_id=?",
                           (encode(refs), status, operation["task_id"], operation["step_id"]))
        self._conn.execute("UPDATE tasks SET revision=revision+1,updated_at=? WHERE task_id=?",
                           (self._now(), operation["task_id"]))

    def operations_pending(self, task_id=None):
        """B40：未落定操作只读清单（prepared/executing/unknown）；task_id=None 时全表。"""
        sql = ("SELECT operation_id, task_id, step_id, run_id, tool, state, created_at, updated_at"
               " FROM tool_operations WHERE state IN ('prepared','executing','unknown')")
        args = ()
        if task_id is not None:
            sql += " AND task_id=?"
            args = (task_id,)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY created_at", args).fetchall()
        return [dict(r) for r in rows]

    def operation_state_counts(self, task_id):
        """B40：按状态计数（/status「已提交/待核实」展示；只读）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT state, COUNT(*) FROM tool_operations WHERE task_id=? GROUP BY state",
                (task_id,)).fetchall()
        return {r[0]: r[1] for r in rows}
