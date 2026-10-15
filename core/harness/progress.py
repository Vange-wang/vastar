"""四项事实共同判定；第二次不拦，进程轮询使用退避和时间窗。"""
import asyncio
import hashlib
import json
import time


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     default=str).encode()).hexdigest()


class ProgressMonitor:
    def __init__(self, *, clock=time.monotonic, poll_window_s=1.0):
        self.clock, self.poll_window_s = clock, poll_window_s
        self.records = {}

    def observe(self, signature, observation, resource_version, step_evidence, *, polling=False):
        now = self.clock()
        facts = (signature, fingerprint(observation), fingerprint(resource_version), fingerprint(step_evidence))
        prev = self.records.get(signature)
        same = prev is not None and facts == prev["facts"]
        count = prev["count"] + 1 if same else 1
        start = prev["since"] if same else now
        # 缺资源事实时无法证明无进展，交次数兜底，不能把 unknown 当相等证据。
        stalled = bool(resource_version) and count >= 3 and (not polling or now - start >= self.poll_window_s)
        result = {"facts": facts, "count": count, "since": start, "stalled": stalled,
                  "backoff_s": min(0.05 * 2 ** min(count - 1, 4), 0.4) if polling and same else 0,
                  "elapsed_s": now - start, "polling": polling}
        self.records[signature] = result
        return result


async def assess_progress(s, context, signature, name, result):
    monitor = getattr(s, "progress_monitor", None)
    if monitor is None:
        monitor = s.progress_monitor = ProgressMonitor()
    evidence = []
    service = getattr(context, "task_service", None)
    if service and s.request.task_id:
        task = service.current(s.request.session_id)
        evidence = [(x["step_id"], x["status"], x["evidence_refs"]) for x in task["steps"]]
    record = monitor.observe(signature, {"status": result.status, "result": result.result,
                                         "reason": result.reason}, result.audit.get("after"), evidence,
                             polling="poll" in name or "process_status" in name)
    if record["backoff_s"] and not record["stalled"]:
        try:
            await asyncio.wait_for(s.request.cancel_event.wait(), timeout=record["backoff_s"])
        except TimeoutError:
            pass
    return record
