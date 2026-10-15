"""Owned native process lifecycle; no PID attachment and no fixed runtime deadline."""
import time

from core.tools.process_output import OutputLimit


def identity(process):
    return {"process_id": process.process.pid, "native_started_at": process.creation_time()}


def verify(entry):
    if identity(entry["process"]) != {k: entry["info"][k] for k in ("process_id", "native_started_at")}:
        raise PermissionError("owned handle identity changed; no PID reattachment")


def pump(entry):
    changed = False
    for stream in ("stdout", "stderr"):
        data = entry["process"].pipes.read(stream)
        if data:
            try:
                entry["output"].append(stream, data)
            except Exception:
                entry["output"].incomplete = True
                raise
            entry["last_progress"] = time.monotonic()
            entry["info"]["progress_bytes"] = entry["output"].total
            changed = True
    return changed


def watch(service, entry):
    status, code = "unknown", None
    try:
        while True:
            with entry["lock"]:
                verify(entry)
                changed = pump(entry)
                code = entry["process"].poll()
                if changed:
                    service.persist(entry)
                if code is not None:
                    status = "completed" if code == 0 else "signal_terminated" if code >= 0xC0000000 else "nonzero_exit"
                    break
                if not service.owner_active(entry):
                    entry["stop_reason"] = "owner_lost"
                if entry["cancel"].is_set():
                    entry["stop_reason"] = "cancelled"
                if entry["stop_reason"]:
                    status = entry["stop_reason"]
                    break
                if time.monotonic() - entry["launched"] >= entry.get("wall_timeout", float("inf")):
                    status = "timeout"
                    break
                if time.monotonic() - entry["last_progress"] >= entry["idle_timeout"]:
                    status = "timeout"
                    break
            time.sleep(0.02)
    except OutputLimit:
        status = "output_limit"
    except Exception:
        status = "unknown"
    finally:
        finish(service, entry, status, code)


def finish(service, entry, status, code):
    with entry["lock"]:
        try:
            entry["process"].job.terminate()
            code = entry["process"].poll()
            if status != "output_limit":
                while pump(entry):
                    pass
        except OutputLimit:
            status = "output_limit"
        except Exception:
            status = "unknown"
        try:
            entry["process"].close()
            entry["profile"].close()
        except Exception:
            status = "unknown"
        entry["info"].update(status=status, exit_code=code,
                              completed_successfully=status == "completed" and code == 0,
                              operation_state="unknown", effects="unknown; exit code is not effect proof",
                              finished_at=service.store._now())
        # A receipt of process exit proves lifecycle, never arbitrary command effects.
        try:
            service.persist(entry, final=True)
        except Exception:
            entry["info"].update(status="unknown", completed_successfully=False, persistence_failed=True)
        finally:
            entry["done"].set()
