"""TaskService 是任务事实的写入口；接受任务与澄清分离，模型只能提出计划。"""
import time
import uuid

from core.harness.task_model import require_revision, validate_acceptance, validate_steps
from memory.repositories.tasks import encode


class TaskService:
    def __init__(self, store, project_id):
        self.store, self.project_id = store, project_id

    def child_plans(self, session_id, config, **kwargs):
        """B44 host-only extension; disabled means no extension tables or dispatch."""
        from core.orchestration.plan_service import PlanService
        return PlanService(self, session_id, config, **kwargs)

    def management(self, config, *, actor='user', clock=None):
        """B50 host-only 管理面：暂停/恢复（先核账）/重派（提 generation）/取消 + 审计留痕。"""
        from core.orchestration.management import ManagementService
        return ManagementService(self, config, actor=actor, clock=clock or time.time)

    def _source(self, session_id, message_id):
        row = self.store._conn.execute(
            "SELECT role,content FROM messages WHERE session_id=? AND message_id=?",
            (session_id, message_id)).fetchone()
        if not row or row["role"] != "user":
            raise ValueError("必须引用当前会话中的用户原始输入")
        return {"session_id": session_id, "message_id": message_id, "text": row["content"]}

    def accept(self, session_id, message_id, *, goal, acceptance, constraints, budget, steps):
        if not goal or not isinstance(acceptance, dict) or not acceptance or not isinstance(budget, dict):
            raise ValueError("接受任务必须保留目标、acceptance、约束、预算和计划")
        if not isinstance(constraints, list):
            raise ValueError("constraints 必须显式给出列表（可为空）")
        steps = validate_steps(steps)
        self._check_mapping(steps, acceptance)
        task_id = "task-" + uuid.uuid4().hex
        with self.store.transaction():
            if self.store.task_for_session(session_id):
                raise ValueError("当前会话已有 Task；本次输入只能澄清，不自动另建目标")
            source = self._source(session_id, message_id)
            now = self.store._now()
            self.store._conn.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                     (task_id, self.project_id, goal, encode(source), encode(constraints),
                                      encode(acceptance), "READY", 1, encode(budget), now, now, 1, "[]"))
            self.store._conn.execute("INSERT INTO session_tasks VALUES(?,?)", (session_id, task_id))
            self.store.task_write_steps(task_id, steps, 1)
            self._revision(task_id, 1, "用户接受任务", {"steps": steps}, "user")
        return self.store.task_get(task_id)

    def attach(self, session_id, task_id):
        with self.store.transaction():
            task = self.store.task_get(task_id)
            if task["project_id"] != self.project_id:
                raise ValueError("跨项目 Task 引用被拒绝")
            current = self.store.task_for_session(session_id)
            if current and current["task_id"] != task_id:
                raise ValueError("会话已关联其他 Task，禁止隐式换目标")
            self.store._conn.execute("INSERT OR IGNORE INTO session_tasks VALUES(?,?)", (session_id, task_id))
        return task

    def clarify(self, session_id, message_id, *, expected_revision):
        with self.store.transaction():
            task = self.current(session_id)
            require_revision(task["revision"], expected_revision)
            source = self._source(session_id, message_id)
            task["clarifications"].append(source)
            self.store._conn.execute("UPDATE tasks SET clarifications=?,revision=revision+1,updated_at=?"
                                     " WHERE task_id=?", (encode(task["clarifications"]), self.store._now(),
                                                          task["task_id"]))
        return self.current(session_id)

    def current(self, session_id):
        task = self.store.task_for_session(session_id)
        if task is None:
            raise ValueError("无结构化 Task；请用户显式接受任务，不能从旧摘要猜造")
        if task["project_id"] != self.project_id:
            raise ValueError("跨项目 Task 引用被拒绝")
        return task

    def propose(self, session_id, proposal, *, expected_revision=None):
        if not isinstance(proposal, dict):
            raise ValueError("proposal 必须为对象")
        with self.store.transaction():
            task = self.current(session_id)
            require_revision(task["current_plan_revision"], expected_revision)
            if task["status"] in {"COMPLETED", "CANCELLED"}:
                raise ValueError("终态 Task 不接受模型修订；新要求须用户显式处理")
            allowed = {"steps", "reason", "acceptance", "removal_explanations"}
            if set(proposal) - allowed or not proposal.get("reason"):
                raise ValueError("提案只允许步骤/原因/验收建议，禁止伪造任务状态")
            validate_acceptance(task["acceptance"], proposal.get("acceptance", task["acceptance"]),
                                proposal.get("removal_explanations"))
            steps = validate_steps(proposal["steps"], task["steps"])
            self._check_mapping(steps, task["acceptance"])
            revision = expected_revision + 1
            self.store.task_write_steps(task["task_id"], steps, revision)
            self._revision(task["task_id"], revision, proposal["reason"], proposal, "model_proposal")
            self.store._conn.execute("UPDATE tasks SET current_plan_revision=?,revision=revision+1,"
                                     "updated_at=? WHERE task_id=?", (revision, self.store._now(), task["task_id"]))
        return self.current(session_id)

    def _revision(self, task_id, revision, reason, proposal, actor):
        self.store._conn.execute("INSERT INTO plan_revisions VALUES(?,?,?,?,?,?)",
                                 (task_id, revision, reason, encode(proposal), actor, self.store._now()))

    @staticmethod
    def _check_mapping(steps, acceptance):
        if any(set(s["acceptance_ids"]) - set(acceptance) for s in steps):
            raise ValueError("步骤引用不存在的验收项")

    def select_step(self, session_id, step_id):
        from core.harness.questions import QuestionService
        with self.store.transaction():
            task = self.current(session_id)
            row = self.store._conn.execute("SELECT recovery_error FROM task_runtime WHERE task_id=?", (task["task_id"],)).fetchone()
            if row and row[0]:
                raise ValueError(f"恢复需核实：{row[0]}；修复后请先 /task resume 核查")
            step = next((s for s in task["steps"] if s["step_id"] == step_id), None)
            if task["status"] in {"COMPLETED", "CANCELLED", "BLOCKED"}:
                raise ValueError("任务终态或返工超限，不能继续执行")
            if not step or step["status"] == "COMPLETED":
                raise ValueError("步骤不存在或已完成")
            if any(s["status"] != "COMPLETED" for s in task["steps"] if s["step_id"] in step["depends_on"]):
                raise ValueError("前置步骤尚未验收")
            if any(q["required"] for q in QuestionService(self).pending(task["task_id"])) and not step["independent"]:
                raise ValueError("等待用户；计划未声明该步骤为独立工作")
            self.store._conn.execute("UPDATE task_steps SET status='READY' WHERE task_id=? AND status IN ('RUNNING','VERIFYING')", (task["task_id"],))
            self.store._conn.execute("UPDATE task_steps SET status='RUNNING' WHERE task_id=? AND step_id=?", (task["task_id"], step_id))
            self.store._conn.execute("UPDATE tasks SET status='RUNNING',revision=revision+1 WHERE task_id=?", (task["task_id"],))
        return self.current(session_id)
