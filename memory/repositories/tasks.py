"""任务仓储：短事务、乐观版本、同事务计划投影。"""
import json


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class TaskRepository:
    def task_for_session(self, session_id):
        with self._lock:
            row = self._conn.execute("SELECT task_id FROM session_tasks WHERE session_id=?",
                                     (session_id,)).fetchone()
            return self.task_get(row[0]) if row else None

    def task_get(self, task_id):
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise ValueError("Task 不存在；禁止静默重建")
            task = dict(row)
            for key in ("source_ref", "constraints", "acceptance", "budget", "clarifications"):
                task[key] = json.loads(task[key])
            task["steps"] = []
            for row in self._conn.execute("SELECT * FROM task_steps WHERE task_id=? ORDER BY rowid", (task_id,)):
                step = dict(row)
                for key in ("depends_on", "evidence_refs", "acceptance_ids"):
                    step[key] = json.loads(step[key])
                task["steps"].append(step)
            task["remaining_work"] = [s["step_id"] for s in task["steps"] if s["status"] != "COMPLETED"]
            return task

    def task_write_steps(self, task_id, steps, revision):
        # 调用者必须持有事务；不要在删/写间 commit。
        if not self._conn.in_transaction:
            raise RuntimeError("task_write_steps 必须在事务内")
        self._conn.execute("DELETE FROM task_steps WHERE task_id=?", (task_id,))
        for step in steps:
            self._conn.execute("INSERT INTO task_steps VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                               (step["step_id"], task_id, step["goal"], encode(step["depends_on"]),
                                step["status"], encode(step["evidence_refs"]), step["remaining_work"],
                                self._owner.owner_id, self._owner.generation, revision,
                                encode(step.get("acceptance_ids", [])), int(step.get("independent", False))))

    def task_link_run(self, session_id, run_id):
        with self.transaction():
            task = self.task_for_session(session_id)
            if task:
                self._conn.execute("UPDATE runs SET task_id=? WHERE run_id=? AND session_id=?",
                                   (task["task_id"], run_id, session_id))
            return task

    def tasks_open(self, project_id):
        """B40：本项目未闭合任务只读清单（COMPLETED/CANCELLED 之外全列；FAILED 照列不美化）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT t.task_id, t.status, t.goal, t.updated_at, t.revision,"
                " (SELECT COUNT(*) FROM task_steps s WHERE s.task_id=t.task_id"
                "  AND s.status!='COMPLETED') AS remaining"
                " FROM tasks t WHERE t.project_id=?"
                " AND t.status NOT IN ('COMPLETED','CANCELLED')"
                " ORDER BY t.updated_at DESC", (project_id,)).fetchall()
        return [dict(r) for r in rows]
