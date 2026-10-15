"""追踪执行体直到真实结束；未知结果自动核实，不取消 to_thread。"""
import asyncio
import time
import weakref
from collections import OrderedDict
from functools import partial

from core.tools import operation_resources as resources
from core.tools.operation_control import OperationCancelled, OperationControl
from core.tools.operation_receipt import ToolOutcome, _contract_from_facts

READ_CLEANUP_SECONDS = 2.5  # 子进程 terminate/kill/reap 各有界；写提交仍按原核实语义。


async def drain(task):
    """调用方/事件循环收尾取消不等于线程停止。保留引用并等真实结束。"""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def thread_call(function, *args, **kwargs):
    # Future 而非可被 asyncio.run 统一取消的 Task；取消只作用于 shield 外层。
    future = asyncio.get_running_loop().run_in_executor(None, partial(function, *args, **kwargs))
    return await drain(future)


class OperationRunner:
    def __init__(self):
        from core.tools.isolated_read import ReadWorkers
        self.active = {}
        self.completed = OrderedDict()
        self.read_workers = ReadWorkers()
        self._read_finalizer = weakref.finalize(self, self.read_workers.close_sync)

    def receipt(self, operation_id):
        entry = self.active.get(operation_id)
        if entry:
            return {**entry['control'].receipt(), "active": True}
        return self.completed.get(operation_id)

    async def wait(self, operation_id):
        entry = self.active.get(operation_id)
        if entry:
            await drain(entry['task'])
        return self.receipt(operation_id)

    async def close(self):
        for entry in list(self.active.values()):
            entry['control'].request_cancel("运行环境关闭")
        for entry in list(self.active.values()):
            await drain(entry['task'])
        await asyncio.to_thread(self.read_workers.close_sync)

    async def run(self, spec, paths, handler, cancel_event, timeout, registry, request_id="", *, operation_id=None):
        control = OperationControl(request_id)
        control.read_workers = self.read_workers
        if operation_id:
            control.operation_id = operation_id
        oid = control.operation_id
        if not resources.claim(oid, paths, read_only=spec.effect == 'read'):
            return ToolOutcome(spec.name, "rejected", "资源仍被未结束操作占用；请等待核实收尾",
                               origin="resource_busy",
                               audit={"operation_state": "resource_busy"})
        started = asyncio.Event()
        loop = asyncio.get_running_loop()
        control.async_cancel = asyncio.Event()
        control.on_cancel = lambda: loop.call_soon_threadsafe(control.async_cancel.set)
        if cancel_event.is_set():
            control.request_cancel()
        control.on_started = lambda: loop.call_soon_threadsafe(started.set)
        starting = asyncio.create_task(started.wait())
        entry = {"control": control, "spec": spec, "registry": registry,
                 "started": time.monotonic_ns()}
        self.active[oid] = entry
        job = asyncio.create_task(self._finish(entry, handler))
        entry['task'] = job
        cancel = asyncio.create_task(cancel_event.wait())
        try:
            done, _ = await asyncio.wait({job, cancel, starting},
                                         return_when=asyncio.FIRST_COMPLETED)
            if job not in done and cancel not in done:
                done, _ = await asyncio.wait({job, cancel}, timeout=timeout,
                                             return_when=asyncio.FIRST_COMPLETED)
            if job in done:
                return job.result()
            reason = "用户取消" if cancel in done else f"工具执行超时（>{timeout:g}s）"
            control.request_cancel(reason)
            if spec.effect == 'read' and spec.cooperative:
                finished, _ = await asyncio.wait({job}, timeout=READ_CLEANUP_SECONDS)
                if finished:
                    return job.result()
            if spec.cooperative and cancel in done:
                return await drain(job)
            return self._unknown(entry, reason,
                                 origin="user_cancel" if cancel in done else "timeout")
        except asyncio.CancelledError:
            control.request_cancel("调用者取消")
            if spec.cooperative:
                await drain(job)
            else:
                self._unknown(entry, "调用者取消", origin="user_cancel")
            raise
        finally:
            cancel.cancel()
            starting.cancel()
            await asyncio.gather(cancel, starting, return_exceptions=True)

    def _unknown(self, entry, reason, *, origin=""):
        control = entry['control']
        control.verification = "running"
        if control.state != "committed":
            control.transition("unknown")
        status = "executed" if control.state == "committed" else "unknown"
        outcome = ToolOutcome(entry['spec'].name, status,
                              reason + "；执行体尚未停止，结果待核实；同资源继续占用",
                              origin=origin,
                              audit={**control.receipt(), "active": True})
        outcome.duration_ms = (time.monotonic_ns() - entry["started"]) // 1_000_000
        if status == "executed":
            outcome.result = "操作已提交；执行体仍在收尾核实中。"
        self._record(entry, outcome)
        return outcome

    async def _finish(self, entry, handler):
        control = entry['control']
        started = time.monotonic_ns()
        # 内层拥有整个 plan/backup/run 执行体；外层取消绝不取消它。
        worker = asyncio.create_task(handler(control))
        try:
            try:
                outcome = await drain(worker)
            except OperationCancelled as exc:
                outcome = ToolOutcome(entry['spec'].name, "cancelled", str(exc),
                                      origin="user_cancel")
            except Exception as exc:
                state = "unknown" if control.commit_started else "failed"
                if control.state != "committed":
                    control.transition(state)
                outcome = ToolOutcome(entry['spec'].name, state, str(exc),
                                      origin="tool_runtime",
                                      audit={"error_class": type(exc).__name__})
            return self._finalize(entry, outcome, started)
        finally:
            resources.release(control.operation_id)
            self.active.pop(control.operation_id, None)

    def _finalize(self, entry, outcome, started):
        control = entry['control']
        if control.state == "unknown" and control.started and outcome.status != "executed":
            before, after = control.facts.get("before"), control.facts.get("after")
            expected = control.facts.get("expected") or {}
            verified = after and not any("verification_error" in item for item in after.values())
            if verified and expected and all(
                    all(after.get(path, {}).get(k) == v for k, v in values.items())
                    for path, values in expected.items()):
                control.transition("committed")
            elif verified and resources.proves_unchanged(before, after):
                control.transition("failed")
                outcome.status = "failed"
        if control.state == "committed":
            if outcome.status != "executed":
                outcome.result = "操作已提交；收尾异常：" + outcome.reason
            outcome.status = "executed"
        elif outcome.status == "executed":
            control.transition("committed")
        elif outcome.status in ("cancelled", "timeout"):
            control.transition("cancelled_before_commit")
        elif control.state not in ("unknown", "cancelled_before_commit"):
            control.transition("failed")
        if control.started:
            control.verification = "unresolved" if control.state == "unknown" else "completed"
        outcome.duration_ms = (time.monotonic_ns() - started) // 1_000_000
        outcome.audit.update(control.receipt())
        outcome.audit['active'] = False
        outcome.contract.update(_contract_from_facts(control.facts))
        self._record(entry, outcome)
        if getattr(control, "ledger", None):
            control.ledger.finish(control, outcome)
        self.completed[control.operation_id] = dict(outcome.audit)
        while len(self.completed) > 256:
            self.completed.popitem(last=False)
        return outcome

    def _record(self, entry, outcome):
        outcome.audit.update(tool_origin=outcome.origin, tool_contract=outcome.contract)
        registry = entry['registry']
        registry.record_call(outcome.name, outcome.status, outcome.duration_ms)
        registry.last_call.update(outcome.audit)
