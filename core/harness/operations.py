"""持久操作账本：先确认意图，后执行；未知结果禁止自动重投。"""
import hashlib
from dataclasses import asdict

from core.harness.reconcile import reconcile_facts
from core.tools.operation_receipt import ToolOutcome
from memory.repositories.tasks import encode


def args_hash(tool, args, paths):
    payload = {"tool": tool, "args": args, "paths": [str(p) for p in paths]}
    return hashlib.sha256(encode(payload).encode()).hexdigest()


def classification(spec):
    return ("read" if spec.effect == "read" else
            "idempotent_write" if spec.retryable and spec.effect == "write" else "non_idempotent")


class OperationLedger:
    def __init__(self, store, *, fault=lambda point: None):
        self.store, self.fault = store, fault

    def replay(self, operation_id, digest, run_id):
        self.store._owner.assert_held()
        old = self.store.operation_get(operation_id)
        if old is None:
            return None
        if old["args_hash"] != digest:
            raise ValueError("同 operation_id 异参数被拒绝；参数改变必须新 ID")
        if old["run_id"] != run_id:
            raise ValueError("操作身份绑定原 Run；跨 Run 请显式 reconcile，不自动重投")
        if old["state"] == "committed" and old["receipt"]:
            self.fault("replay")
            return ToolOutcome(**old["receipt"])
        if old["state"] == "not_committed":
            return ToolOutcome(old["tool"], "rejected",
                               "已核实未提交；旧操作不重放，请显式选择步骤并使用新 operation_id",
                               origin="operation_ledger", audit={"operation_id": operation_id,
                                                                   "reconcile": old["reconcile_result"]})
        return ToolOutcome(old["tool"], "unknown", "已有未完成意图；必须 reconcile，unknown 不自动重试",
                           origin="operation_ledger", audit={"operation_id": operation_id,
                                                               "operation_state": old["state"]})

    def bind(self, control, spec, digest):
        control.ledger = self
        control.durable_spec = spec
        control.args_hash = digest
        control.durable_prepared = False
        control.before_commit = lambda: self.begin_commit(control)

    def prepare(self, control):
        store, spec = self.store, control.durable_spec
        if store._conn.in_transaction:
            raise ValueError("prepared 必须独立事务确认，禁止嵌入未提交事务")
        with store.transaction():
            if store.operation_get(control.operation_id):
                raise ValueError("操作 ID 已存在；拒绝并发重投")
            run = store._conn.execute("SELECT task_id,step_id FROM runs WHERE run_id=?",
                                      (control.request_id,)).fetchone()
            task_id, step_id = (run[0], run[1]) if run else (None, None)
            if task_id and step_id is None:
                raise ValueError("结构化 Task 写入必须显式选择 step_id")
            facts = dict(control.facts)
            approval = {k: facts.get(k) for k in ("permission", "approval_source")}
            now = store._now()
            store._conn.execute("INSERT INTO tool_operations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (control.operation_id, task_id, step_id, control.request_id, spec.name,
                                 "1", classification(spec), control.args_hash, encode(facts["before"]),
                                 encode(approval), "prepared", None, store.fencing_generation,
                                 store._owner.owner_id, encode(facts), None, None, now, now))
        control.durable_prepared = True
        self.fault("prepared")  # 此处其他 SQLite 连接已能读取意图。

    def executing(self, control):
        with self.store.transaction():
            self.store._conn.execute("UPDATE tool_operations SET state='executing',updated_at=?"
                                     " WHERE operation_id=? AND fencing_generation=?",
                                     (self.store._now(), control.operation_id, self.store.fencing_generation))
        self.fault("executing")

    def begin_commit(self, control):
        self.store._owner.assert_held()
        if self.store._conn.in_transaction:
            raise ValueError("提交前资源证据必须独立事务确认")
        if control.durable_spec.name == "move_file":
            before = control.facts["before"]
            paths = list(before)
            if len(paths) == 2 and before[paths[0]].get("sha256"):
                control.facts["expected"] = {paths[0]: {"exists": False}, paths[1]: before[paths[0]]}
        with self.store.transaction():
            row = self.store.operation_get(control.operation_id)
            if row is None or row["fencing_generation"] != self.store.fencing_generation:
                raise ValueError("提交代次过期或 prepared 意图缺失")
            self.store._conn.execute("UPDATE tool_operations SET facts=? WHERE operation_id=?",
                                     (encode(control.facts), control.operation_id))
        self.fault("before_commit")

    def finish(self, control, outcome):
        if not control.durable_prepared:
            return
        if self.store.operation_get(control.operation_id) is None:
            return  # 权限/计划阶段拒绝，还未接受操作意图。
        self.fault("before_receipt")
        state = "committed" if outcome.status == "executed" else (
            "unknown" if control.commit_started and control.state != "committed" else "failed")
        self.store.operation_finish(control.operation_id, state, asdict(outcome), control.facts,
                                     fault=self.fault)
        self.fault("after_receipt")

    def reconcile(self, operation_id):
        self.store._owner.assert_held()
        operation = self.store.operation_get(operation_id)
        if operation is None:
            raise ValueError("操作不存在；不能创建空回执冒充恢复")
        result = reconcile_facts(operation)
        receipt = operation["receipt"]
        if result["status"] == "committed" and receipt is None:
            receipt = asdict(ToolOutcome(operation["tool"], "executed", result="已依据目标 hash 与备份核实提交",
                                         audit={"operation_id": operation_id, "reconcile": result}))
        state = result["status"]
        if operation["state"] != "committed":
            self.store.operation_finish(operation_id, state, receipt, operation["facts"], reconciliation=result)
        return result
