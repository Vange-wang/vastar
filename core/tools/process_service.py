"""Task-owned jobs over B27 isolation, B16 artifacts and B18 prepared intents."""
import threading
import time
import uuid
from dataclasses import asdict

from core.harness.operations import args_hash
from core.tools.operation_control import OperationControl
from core.tools.operation_receipt import ToolOutcome
from core.tools.process_lifecycle import identity, verify, watch
from core.tools.process_output import ProcessOutput
from core.tools.report_transport import output_for
from memory.repositories.tasks import encode


class ProcessService:
    def __init__(self, sandbox, ledger, artifacts):
        self.sandbox, self.ledger, self.artifacts = sandbox, ledger, artifacts
        self.store = ledger.store
        self.entries, self.lock, self.closed = {}, threading.RLock(), False

    def require_task(self, task_id, run_id):
        task = self.store.task_get(task_id)
        if task["status"] not in {"READY", "RUNNING", "VERIFYING"}:
            raise ValueError("Task is not active")
        with self.store._lock:
            run = self.store._conn.execute("SELECT task_id,step_id FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not run or run["task_id"] != task_id or not run["step_id"]:
            raise ValueError("process requires an explicitly selected Task/Step/Run")
        if not any(s["step_id"] == run["step_id"] for s in task["steps"]):
            raise ValueError("selected step no longer exists")

    def start(self, script, *, task_id, run_id, spec, operation_id=None,
              approved=False, encoding="utf-8", idle_timeout=300, cancel=None, payload=None):
        self.sandbox.policy.authorize(approved=approved)
        if not self.sandbox.preflight()["execution_effective"]:
            raise PermissionError("non-sandbox/unavailable backend; refused")
        if not isinstance(script, str) or not script or len(script) > 8192:
            raise ValueError("script must contain 1..8192 characters")
        if encoding not in {"utf-8", "gbk"} or type(idle_timeout) not in (int, float) or not 0 < idle_timeout <= 86400:
            raise ValueError("encoding utf-8/gbk and idle_timeout in (0,86400] required")
        self.require_task(task_id, run_id)
        self.artifacts._require_dir()
        args = {"script": script, "encoding": encoding, "idle_timeout": idle_timeout, "task_id": task_id}
        if payload is not None:
            args["payload"] = payload.manifest
        digest = args_hash(spec.name, args, [])
        with self.lock:
            if self.closed or sum(not e["done"].is_set() for e in self.entries.values()) >= 4:
                raise RuntimeError("process service closed or active job limit reached")
            if operation_id:
                replay = self.ledger.replay(operation_id, digest, run_id)
                if replay:
                    return replay
            if self.sandbox._active or self.sandbox._unresolved:
                raise RuntimeError("B27 backend has an active/unresolved command")
            control = OperationControl(run_id)
            control.operation_id = operation_id or control.operation_id
            self.ledger.bind(control, spec, digest)
            control.facts = {"before": {}, "permission": "ALLOW", "approval_source": "user_allow"}
            if payload is not None:
                control.facts.update(external_evidence=True, document=payload.manifest)
            self.ledger.prepare(control)
            self.ledger.executing(control)
            return self._launch(script, task_id, control, encoding, idle_timeout, cancel, payload)

    def _launch(self, script, task_id, control, encoding, idle_timeout, cancel, payload=None):
        from core.harness.windows_process import NativeProcess
        from core.harness.windows_sandbox import ContainerProfile

        profile = process = None
        cancel = cancel or threading.Event()
        job_id = "job-" + uuid.uuid4().hex
        info = {"job_id": job_id, "owner": task_id, "runtime_owner": self.store._owner.owner_id,
                "operation_id": control.operation_id, "started_at": self.store._now(),
                "process_id": None, "cwd": None, "status": "preparing",
                "completed_successfully": False, "exit_code": None}
        entry = {"info": info, "control": control, "lock": threading.RLock(),
                 "output": output_for(payload, self.artifacts, job_id, encoding), "cancel": cancel,
                 "done": threading.Event(), "stop_reason": "", "idle_timeout": idle_timeout}
        try:
            self.persist(entry)
            if cancel.is_set():
                raise InterruptedError("cancelled before launch")
            profile = ContainerProfile()
            profile.install_runtime()
            if payload is not None:
                payload.stage(profile)
                info["resource_limits"] = payload.manifest["limits"]
                entry["output"].MAX_BYTES = payload.manifest["limits"]["output_bytes"]
                entry["wall_timeout"] = payload.manifest["limits"]["wall_seconds"]
            process = NativeProcess(profile, script, cancel=cancel, capture=True)
            info.update(identity(process), cwd=str(profile.work), status="started", operation_state="executing")
            entry.update(profile=profile, process=process, last_progress=time.monotonic(), launched=time.monotonic())
            self.persist(entry)
            self.entries[job_id] = entry
            for old_id, old in list(self.entries.items()):
                if len(self.entries) > 32 and old["done"].is_set():
                    del self.entries[old_id]
            entry["thread"] = threading.Thread(target=watch, args=(self, entry), daemon=False)
            result = self.outcome(entry, "start_process")
            entry["thread"].start()
            return result
        except BaseException as exc:
            issued = process is not None or getattr(exc, "execution_issued", False)
            try:
                if process:
                    process.close()
                if profile:
                    profile.close()
            finally:
                info.update(status="unknown" if issued else "cancelled" if cancel.is_set() else "failed",
                            operation_state="unknown" if issued else "not_committed")
                self.persist(entry, final=True)
            return self.outcome(entry, "start_process")

    def persist(self, entry, final=False):
        control = entry["control"]
        control.facts["process"] = {**entry["info"], "output": entry["output"].snapshot()}
        if final:
            outcome = self.outcome(entry, "start_process")
            self.store.operation_finish(control.operation_id, entry["info"]["operation_state"],
                                         asdict(outcome), control.facts)
        else:
            with self.store.transaction():
                self.store._conn.execute("UPDATE tool_operations SET facts=?,updated_at=? WHERE operation_id=?",
                                         (encode(control.facts), self.store._now(), control.operation_id))

    def owner_active(self, entry):
        self.store._owner.assert_held()
        return self.store.task_get(entry["info"]["owner"])["status"] not in {
            "COMPLETED", "CANCELLED", "FAILED", "CANCEL_REQUESTED"}

    def outcome(self, entry, name):
        name = entry["control"].durable_spec.name
        info = dict(entry["info"])
        info["truncated"] = entry["output"].snapshot()["truncated"]
        info["rollback"] = "stop/reconcile before adapter rollback; issued commands cannot be undone by code rollback"
        status = "executed" if info["status"] in {"started", "completed"} else (
            info["status"] if info["status"] in {"failed", "cancelled"} else "unknown")
        return ToolOutcome(name, status,
                           result=encode(info), audit=info)

    def lookup(self, job_id, owner, started_at):
        with self.lock:
            entry = self.entries.get(job_id)
        if entry:
            if entry["info"]["owner"] != owner or entry["info"]["started_at"] != started_at:
                raise PermissionError("job owner/start time mismatch")
            return entry
        with self.store._lock:
            row = self.store._conn.execute(
                "SELECT operation_id FROM tool_operations WHERE json_extract(facts,'$.process.job_id')=?",
                (job_id,)).fetchone()
        if row is None:
            raise ValueError("job missing; never attach by PID")
        op = self.store.operation_get(row[0])
        info = op["facts"]["process"]
        if info["owner"] != owner or info["started_at"] != started_at:
            raise PermissionError("job owner/start time mismatch")
        output = ProcessOutput(self.artifacts, job_id, info["output"]["encoding"], info.pop("output"))
        if info["status"] in {"preparing", "started"}:
            info.update(status="process_missing", operation_state="unknown", completed_successfully=False,
                        recovery="No live owned handle; PID is never reused to attach")
            facts = {**op["facts"], "process": {**info, "output": output.snapshot()}}
            self.store.operation_finish(op["operation_id"], "unknown", op["receipt"], facts,
                                         reconciliation={"status": "unknown", "evidence": info})
        return {"info": info, "output": output, "lock": threading.RLock(), "done": None}

    def read(self, job_id, *, owner, started_at, cursor=0, max_bytes=16384):
        entry = self.lookup(job_id, owner, started_at)
        with entry["lock"]:
            return {**entry["info"], **entry["output"].read(cursor, max_bytes)}

    def terminate(self, job_id, *, owner, started_at, reason="signal_terminated"):
        entry = self.lookup(job_id, owner, started_at)
        if entry["done"] is not None:
            with entry["lock"]:
                if not entry["done"].is_set():
                    verify(entry)
                    entry["stop_reason"] = reason
            entry["done"].wait()
        return self.read(job_id, owner=owner, started_at=started_at, max_bytes=1)

    def assert_can_rollback(self):
        with self.store._lock:
            pending = self.store._conn.execute(
                "SELECT 1 FROM tool_operations WHERE tool IN ('start_process','extract_document','generate_report',"
                "'analyze_spreadsheet','browser_navigate') "
                "AND state NOT IN ('committed','not_committed') OR tool IN ('browser_click','browser_fill') "
                "AND state IN ('prepared','executing','unknown') LIMIT 1").fetchone()
        if pending:
            raise RuntimeError("stop/reconcile operations before adapter rollback")

    def close(self):
        with self.lock:
            self.closed = True
            entries = list(self.entries.values())
            for entry in entries:
                entry["cancel"].set()
        for entry in entries:
            entry["thread"].join()
