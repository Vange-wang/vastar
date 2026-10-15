"""Vastar runtime component."""
import asyncio
import itertools
import time
from dataclasses import dataclass
from uuid import uuid4

from core.orchestration.contracts import PlanRevision, TaskResult, WorkerProfile
from core.orchestration.scheduler import Scheduler
from core.orchestration.validation import terminal


@dataclass(frozen=True)
class ChildJob:
    """一个子委派项：计划版本 + 角色 + 步骤 + 验收回调（completed 须经 accept 核实）。"""
    plan: PlanRevision
    profile: WorkerProfile
    step_id: str
    accept: object = None

    def __post_init__(self):
        if type(self.plan) is not PlanRevision or type(self.profile) is not WorkerProfile:
            raise ValueError('worker_child_job_invalid')


class _Child:
    """子项运行记录（监督器私有）：任务句柄 / 事件流 / 已产出片段 / 子 Run 映射。"""

    __slots__ = ('child_id', 'job', 'envelope', 'task', 'events', 'chunks', 'result',
                 'run_id', 'session_id', 'finished_order', 'worker',
                 'detached', 'owner', 'admitted_at', 'last_progress')

    def __init__(self, child_id, job):
        self.child_id = child_id
        self.job = job
        self.envelope = None
        self.task = None
        self.events = []
        self.chunks = []
        self.result = None
        self.run_id = None
        self.session_id = None
        self.finished_order = None
        self.worker = None
        # B46：独立所有权（detach 后父取消不连带）与进展打点（watchdog 依据）
        self.detached = False
        self.owner = None
        self.admitted_at = time.monotonic()
        self.last_progress = None


class ChildSupervisor:
    """只读子 Agent 委派监督器（fan-out/fan-in/状态/取消；依赖 B42 Scheduler 准入）。"""

    def __init__(self, scheduler: Scheduler) -> None:
        if type(scheduler) is not Scheduler:
            raise ValueError('worker_supervisor_scheduler_invalid')
        self._scheduler = scheduler
        self._children: dict[str, _Child] = {}
        self._finished_seq = itertools.count(1)
        self._accepting = True   # B46：派发闸——取消树先关闸再传播（先停止派发）

    # ---- delegate（fan-out） ---------------------------------------------------

    def _readonly_gate(self, envelope) -> str | None:
        """只读闸：请求非 read 效果工具 → 返回拒绝原因（准入前、不触达 provider）。"""
        for name in envelope.allowed_tools:
            spec = self._scheduler.tools.registry.get(name)
            if (spec is not None and self._scheduler.workspaces is not None
                    and envelope.write_scope and name in {'write_file', 'edit_file', 'mkdir'}):
                continue
            if spec is None or spec.effect != 'read':
                return f'worker_readonly_tool_refused:{name}'
        return None

    def delegate(self, jobs: tuple) -> tuple[str, ...]:
        if type(jobs) is not tuple or not jobs:
            raise ValueError('worker_delegate_jobs_invalid')
        ids = []
        for job in jobs:
            if type(job) is not ChildJob:
                raise ValueError('worker_delegate_jobs_invalid')
            child_id = uuid4().hex
            rec = _Child(child_id, job)
            self._children[child_id] = rec
            ids.append(child_id)
            self._admit_one(rec)
        return tuple(ids)

    def _admit_one(self, rec: _Child) -> None:
        job = rec.job
        parent = self._scheduler.parent
        envelope = next((s for s in job.plan.steps if s.step_id == job.step_id), None)
        if envelope is None:
            rec.result = TaskResult('blocked', '', (), (), (job.step_id,),
                                    ('worker_step_not_in_plan',), (), parent.base_revision,
                                    parent.plan_revision, parent.owner_generation)
            return
        rec.envelope = envelope
        if not self._accepting:
            # B46：派发闸已关（取消树第一步）——不触达 provider，显式 blocked
            rec.result = terminal(envelope, 'blocked', 'worker_dispatch_closed')
            return
        refused = self._readonly_gate(envelope)
        if refused is not None:
            rec.result = terminal(envelope, 'blocked', refused)
            return
        try:
            worker, env = self._scheduler.admit(job.plan, job.profile, job.step_id,
                                                accept=job.accept, emit=self._emitter(rec))
        except ValueError as exc:
            rec.result = terminal(envelope, 'blocked', str(exc))   # 准入失败只记本项
            return
        rec.worker = worker
        rec.task = asyncio.create_task(self._drive(rec, worker, env))

    # ---- 子运行驱动与事件映射 ----------------------------------------------------

    def _emitter(self, rec: _Child):
        async def emit(event):
            if rec.run_id is None:
                rec.run_id, rec.session_id = event.run_id, event.session_id
            rec.events.append(event)
            rec.last_progress = time.monotonic()   # B46 watchdog 的实际进展依据
            if event.type == 'content.delta':
                rec.chunks.append(str(event.payload.get('text', '')))
        return emit

    async def _drive(self, rec: _Child, worker, envelope) -> None:
        try:
            rec.result = await worker.run(envelope)
        except asyncio.CancelledError:
            rec.result = terminal(envelope, 'cancelled', 'worker_parent_cancelled',
                                  summary=''.join(rec.chunks))   # 取消也留部分痕迹
        except Exception as exc:
            rec.result = terminal(envelope, 'failed',
                                  f'worker_supervisor_failure:{type(exc).__name__}',
                                  summary=''.join(rec.chunks))
        finally:
            rec.finished_order = next(self._finished_seq)

    # ---- wait / status / cancel / read_report -----------------------------------

    async def wait(self, *, timeout: float | None = None) -> dict:
        pending = [rec.task for rec in self._children.values()
                   if rec.result is None and rec.task is not None]
        if pending:
            await asyncio.wait(pending, timeout=timeout)   # 到点即止，不取消在跑子项
        return {cid: rec.result for cid, rec in self._children.items()
                if rec.result is not None}

    def status(self) -> tuple[dict, ...]:
        return tuple({'child_id': cid, 'step_id': rec.job.step_id,
                      'state': 'running' if rec.result is None else rec.result.status,
                      'run_id': rec.run_id, 'events': len(rec.events),
                      'finished_order': rec.finished_order,
                      'detached': rec.detached, 'owner': rec.owner,
                      'admitted_at': rec.admitted_at, 'last_progress': rec.last_progress}
                     for cid, rec in self._children.items())

    # ---- B46：派发闸与独立所有权 -------------------------------------------------

    @property
    def dispatch_open(self) -> bool:
        return self._accepting

    def close_dispatch(self) -> None:
        """取消树第一步：停止新派发（后续 delegate 一律 blocked，不触达 provider）。"""
        self._accepting = False
        if self._scheduler.plans is not None:
            from core.orchestration.dispatch_gate import close
            close(self._scheduler.budget_pool.bind(self._scheduler.plans,
                                                   ttl=self._scheduler.lease_seconds))

    def detach(self, child_id: str, *, owner: str) -> None:
        """独立后台任务显式转移所有权：父级取消树**不连带** detached 子项。

        显式 cancel(child_id) 仍可取消（点名 ≠ 连带）。持久租约原子划拨预算。"""
        rec = self._children.get(child_id)
        if rec is None:
            raise ValueError('worker_child_unknown')
        if type(owner) is not str or not owner.strip():
            raise ValueError('worker_detach_owner_invalid')
        if rec.result is not None or rec.task is None or rec.task.done():
            raise ValueError('worker_child_not_running')
        if not self._accepting:
            raise ValueError('worker_dispatch_closed')
        lease = getattr(rec.worker, 'lease', None)
        if lease is not None:
            lease.detach(owner)
        elif not rec.detached:
            self._scheduler.budget_pool.detach_worker(rec.worker, owner)
        rec.detached, rec.owner = True, owner

    async def cancel(self, child_id: str | None = None) -> int:
        if child_id is not None and child_id not in self._children:
            raise ValueError('worker_child_unknown')
        targets = [rec for rec in self._children.values()
                   if (child_id is None or child_id == rec.child_id)
                   and (child_id is not None or not rec.detached)   # B46：父取消不连带独立子项
                   and rec.task is not None and not rec.task.done()]
        if child_id is not None and not targets:
            raise ValueError('worker_child_not_running')
        for rec in targets:
            if getattr(rec.worker, 'quota', None) is not None:
                rec.worker.quota.pause()
            rec.task.cancel()
        if targets:
            await asyncio.gather(*(rec.task for rec in targets), return_exceptions=True)
        for rec in targets:
            if rec.result is None:
                rec.result = rec.worker.cancel_before_start(rec.envelope)
                if rec.result is not None:
                    rec.finished_order = next(self._finished_seq)
        return len(targets)

    def read_report(self, child_id: str) -> str:
        """父级按需读取子原始全文（不随 wait 回灌；未知 child_id → 显式报错）。"""
        rec = self._children.get(child_id)
        if rec is None:
            raise ValueError('worker_child_unknown')
        return ''.join(rec.chunks)
