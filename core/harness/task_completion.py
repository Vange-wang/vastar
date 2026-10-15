"""Recheck frozen outcomes before completion; persist validation and projection atomically."""
import json
import uuid

from memory.repositories.tasks import encode


def completion_results(validator, task, step, results):
    """External evidence reads happen outside the short projection transaction."""
    checked = {step["step_id"]: results}
    others = [s for s in task["steps"] if s["step_id"] != step["step_id"]]
    if not all(r["passed"] for r in results.values()) or any(s["status"] != "COMPLETED" for s in others):
        return checked
    for previous in others:
        recorded = _recorded_results(validator.store, task["task_id"], previous)
        checked[previous["step_id"]] = {}
        for key in previous["acceptance_ids"]:
            old = recorded.get(key)
            result = (validator.check(task["acceptance"][key], old.get("evidence", {}))
                      if isinstance(old, dict) and old.get("passed") is True else
                      {"passed": False, "reason": "已完成步骤缺少可信验收记录；需要重新验收", "evidence": {}})
            checked[previous["step_id"]][key] = result
    return checked


def _recorded_results(store, task_id, step):
    for ref in reversed(step["evidence_refs"]):
        if not isinstance(ref, str) or not ref.startswith("validation-"):
            continue
        row = store._conn.execute("SELECT result FROM task_validations WHERE validation_id=? AND task_id=? AND step_id=?",
                                  (ref, task_id, step["step_id"])).fetchone()
        # A missing/corrupt latest record must not silently fall back to stale evidence.
        try:
            value = json.loads(row[0]) if row else None
            return value if isinstance(value, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _write_step(store, task, step, results, reviewer, waiting, rework):
    passed = bool(results) and all(r["passed"] for r in results.values())
    failures = rework.get(step["step_id"], 0) + (not passed)
    rework[step["step_id"]] = failures
    status = ("VERIFYING" if waiting else "COMPLETED") if passed else (
        "RUNNING" if failures <= 2 else "BLOCKED")
    vid = "validation-" + uuid.uuid4().hex
    store._conn.execute("INSERT INTO task_validations VALUES(?,?,?,?,?,?,?)",
                        (vid, task["task_id"], step["step_id"], task["current_plan_revision"],
                         encode(results), encode(reviewer), store._now()))
    refs = step["evidence_refs"] + ([vid] if passed else [])
    store._conn.execute("UPDATE task_steps SET status=?,evidence_refs=?,remaining_work=?,revision=revision+1"
                        " WHERE step_id=? AND task_id=?",
                        (status, encode(refs), "" if passed else step["goal"], step["step_id"], task["task_id"]))
    return {"passed": passed, "results": results, "validation_id": vid,
            "failures": failures, "status": status}


def commit_validation(store, task, step, checked, reviewer, waiting):
    with store.transaction():
        current = store.task_get(task["task_id"])
        if current["revision"] != task["revision"]:
            raise ValueError("验收期间任务版本变化，必须重验")
        store._conn.execute("INSERT OR IGNORE INTO task_runtime(task_id) VALUES(?)", (task["task_id"],))
        row = store._conn.execute("SELECT rework FROM task_runtime WHERE task_id=?", (task["task_id"],)).fetchone()
        rework = json.loads(row[0])
        records = {}
        for candidate in task["steps"]:
            sid = candidate["step_id"]
            if sid in checked:
                records[sid] = _write_step(store, task, candidate, checked[sid],
                                           reviewer if sid == step["step_id"] else None,
                                           waiting and sid == step["step_id"], rework)
        store._conn.execute("UPDATE task_runtime SET rework=? WHERE task_id=?", (encode(rework), task["task_id"]))
        fresh = store.task_get(task["task_id"])
        covered = {k for s in fresh["steps"] if s["status"] == "COMPLETED" for k in s["acceptance_ids"]}
        complete = not fresh["remaining_work"] and covered == set(task["acceptance"])
        blocked = any(r["status"] == "BLOCKED" for r in records.values())
        overall = "COMPLETED" if complete else ("BLOCKED" if blocked else "READY")
        missing = sorted(set(task["acceptance"]) - covered) if not fresh["remaining_work"] else []
        if (waiting or missing) and overall != "BLOCKED":
            overall = "WAITING_INPUT"
        store._conn.execute("UPDATE tasks SET status=?,revision=revision+1 WHERE task_id=?", (overall, task["task_id"]))
    active = records[step["step_id"]]
    return {"passed": all(r["passed"] for r in records.values()) and not missing,
            "results": active["results"], "rework_used": min(max(r["failures"] for r in records.values()), 2),
            "status": overall, "validation_id": active["validation_id"], "reviewer_advisory": reviewer,
            "rechecked_steps": records, "missing_acceptance": missing}
