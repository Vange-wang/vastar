"""一致边界检查点：指纹、水位、未决操作和预算事实齐备后才提交。"""
import hashlib
import json
import re
import uuid
from dataclasses import asdict

from memory.repositories.tasks import encode


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def watermark(store, limit=None):
    clause = " WHERE rowid<=?" if limit is not None else ""
    rows = store._conn.execute("SELECT rowid,event_id,payload FROM events" + clause + " ORDER BY rowid",
                               (limit,) if limit is not None else ()).fetchall()
    return {"rowid": rows[-1][0] if rows else 0, "count": len(rows), "hash": digest([list(r) for r in rows])}


def artifact_references(artifacts, value):
    refs = []
    for uri in sorted(set(re.findall(r"artifact://[\w/-]+#[a-f0-9]+", encode(value)))):
        artifact_id, version, scope = artifacts._parse_ref(uri)
        meta, _ = artifacts.get(artifact_id, version)
        if meta["scope"] != scope:
            raise ValueError("检查点证据 scope 不符")
        refs.append(meta)
    return refs


class CheckpointService:
    def __init__(self, store, artifacts, *, fault=lambda point: None):
        self.store, self.artifacts, self.fault = store, artifacts, fault
        artifacts.set_checkpoint_ref_provider(self.references)

    def save(self, task_id, *, context, budget_used, budget_reserved, step_id=None):
        store = self.store
        store._owner.assert_held()
        if store._conn.in_transaction:
            raise ValueError("检查点只允许在事务外安全边界提交")
        for value in (budget_used, budget_reserved):
            if not isinstance(value, dict):
                raise ValueError("检查点必须显式给出预算已用与预留")
        task = store.task_get(task_id)
        meta = self.artifacts.put(encode(context).encode(), source_ref="", retention="permanent")
        checkpoint_id = "checkpoint-" + uuid.uuid4().hex
        with store.transaction():
            current = store.task_get(task_id)
            if current["revision"] != task["revision"]:
                raise ValueError("快照期间 Task 已变化；拒绝不一致检查点")
            operations = [dict(r) for r in store._conn.execute(
                "SELECT operation_id,state,facts,receipt FROM tool_operations WHERE task_id=?", (task_id,))]
            validations = [dict(r) for r in store._conn.execute(
                "SELECT * FROM task_validations WHERE task_id=? ORDER BY rowid", (task_id,))]
            body = {"validation_records": validations,
                    "operation_versions": {r["operation_id"]: digest(json.loads(r["facts"])) for r in operations},
                    "task_id": task_id, "step_id": step_id, "plan_revision": task["current_plan_revision"],
                    "task_revision": task["revision"], "task": task, "event_watermark": watermark(store),
                    "context_ref": asdict(meta), "memory_revision": store._conn.execute(
                        "SELECT COALESCE(MAX(revision),0) FROM memory_revisions").fetchone()[0],
                    "pending_operations": [r["operation_id"] for r in operations if r["state"] in
                                           {"prepared", "executing", "unknown"}],
                    "budget_used": budget_used, "budget_reserved": budget_reserved,
                    "artifact_refs": artifact_references(self.artifacts, [task, context, operations, validations]),
                    "file_snapshots": {path: facts for r in operations if r["state"] == "committed"
                                       for path, facts in json.loads(r["facts"]).get("after", {}).items()}}
            for validation in validations:
                for result in json.loads(validation["result"]).values():
                    if result["passed"]:
                        body["file_snapshots"].update(result.get("observed", {}))
            store._conn.execute("INSERT INTO checkpoints VALUES(?,?,?,?,?,?)",
                                 (checkpoint_id, task_id, encode(body), digest(body), "committed", store._now()))
            self.fault("checkpoint_before_commit")
        self.fault("checkpoint_committed")
        return {"checkpoint_id": checkpoint_id, **body}

    def references(self, artifact_id, version):
        with self.store._lock:
            rows = self.store._conn.execute("SELECT body FROM checkpoints WHERE state='committed'").fetchall()
            for row in rows:
                body = json.loads(row[0])
                refs = [body["context_ref"], *body.get("artifact_refs", [])]
                if any(ref["artifact_id"] == artifact_id and ref["version"] == version for ref in refs):
                    return True
            return False
