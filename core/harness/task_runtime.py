"""跨 Run 的 Task 预算：先持久预留，确认结算后释放；崩溃未知额不可重置。"""
import json

from memory.repositories.tasks import encode


class TaskRuntime:
    def __init__(self, tasks):
        self.tasks, self.store = tasks, tasks.store

    def snapshot(self, task_id):
        with self.store.transaction():
            self.store._conn.execute("INSERT OR IGNORE INTO task_runtime(task_id) VALUES(?)", (task_id,))
            row = dict(self.store._conn.execute("SELECT * FROM task_runtime WHERE task_id=?", (task_id,)).fetchone())
            row["receipts"] = json.loads(row["receipts"])
            row["estimated_tokens"] = sum(x["estimated_tokens"] for x in row["receipts"])
            return row

    def reserve(self, session_id, run_id):
        from core.harness.questions import QuestionService
        with self.store.transaction():
            task = self.tasks.current(session_id)
            from core.budget_topup.single import effective_budget
            task['budget'] = effective_budget(self.store, task)
            steps = [s for s in task["steps"] if s["status"] in {"RUNNING", "VERIFYING"}]
            if len(steps) != 1 or task["status"] in {"COMPLETED", "BLOCKED", "CANCELLED"}:
                raise ValueError("请先显式选择一个可执行步骤")
            step = steps[0]
            if any(s["status"] != "COMPLETED" for s in task["steps"] if s["step_id"] in step["depends_on"]):
                raise ValueError("步骤依赖变化或未完成，拒绝派发")
            if any(q["required"] for q in QuestionService(self.tasks).pending(task["task_id"])) and not step["independent"]:
                raise ValueError("等待用户；没有获计划允许的独立工作")
            if self.store._conn.execute("SELECT 1 FROM tool_operations WHERE task_id=? AND state IN ('prepared','executing','unknown')", (task["task_id"],)).fetchone():
                raise ValueError("未决操作须先 reconcile，禁止自动重试")
            row = self.snapshot(task["task_id"])
            if row["recovery_error"]:
                raise ValueError(f"恢复需核实：{row['recovery_error']}")
            limit = task["budget"].get("max_calls")
            if type(limit) is not int or limit <= 0:
                raise ValueError("任务缺少明确调用预算；needs_input")
            if row["active_run"] or row["reserved_calls"]:
                raise ValueError("存在未结算 Run 预算，必须核实；不能清零重启")
            remaining = limit - row["used_calls"]
            if remaining <= 0:
                raise ValueError("Task 调用预算已耗尽")
            token_limit = task["budget"].get("max_estimated_tokens")
            if token_limit is not None and (type(token_limit) is not int or token_limit <= 0):
                raise ValueError("任务估算预算必须为正整数")
            tokens = None if token_limit is None else token_limit - row["estimated_tokens"]
            if tokens is not None and tokens <= 0:
                raise ValueError("Task 估算 token 预算已耗尽")
            self.store._conn.execute("UPDATE task_runtime SET active_run=?,reserved_calls=? WHERE task_id=?",
                                     (run_id, remaining, task["task_id"]))
            self.store._conn.execute("UPDATE runs SET step_id=? WHERE run_id=? AND task_id=?",
                                     (step["step_id"], run_id, task["task_id"]))
        return step["step_id"], remaining, tokens

    def settle(self, task_id, result):
        with self.store.transaction():
            row = self.snapshot(task_id)
            if row["active_run"] != result.run_id:
                return  # 相同 Run 不能重复结算；被闸门拒绝的 Run 不消耗预算。
            row["receipts"].append({"run_id": result.run_id, "calls": result.model_calls,
                "estimated_tokens": result.budget_summary.get("estimated_tokens", 0),
                "usage": result.usage_totals, "cost": result.cost_summary,
                "call_records": result.call_records})
            self.store._conn.execute("UPDATE task_runtime SET used_calls=used_calls+?,reserved_calls=0,"
                                     "active_run=NULL,receipts=? WHERE task_id=?",
                                     (result.model_calls, encode(row["receipts"]), task_id))

    def checkpoint_budget(self, task_id):
        row = self.snapshot(task_id)
        return ({"calls": row["used_calls"], "estimated_tokens": row["estimated_tokens"],
                 "receipts": row["receipts"]},
                {"calls": row["reserved_calls"], "run_id": row["active_run"]})
