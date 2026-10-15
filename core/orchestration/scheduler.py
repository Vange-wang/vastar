"""Host-only admission, with optional B44 persisted acceptance dependency gates."""
from dataclasses import dataclass
from threading import RLock

from core.orchestration.config import OrchestrationConfig
from core.orchestration.contracts import Permissions, PlanRevision, WorkerProfile, strings
from core.orchestration.permissions import intersect
from core.orchestration.worker_runner import ReActWorker


@dataclass(frozen=True)
class ParentAuthority:
    task_id: str
    step_id: str
    base_revision: str
    plan_revision: int
    owner_generation: int
    permissions: Permissions
    evidence_refs: tuple[str, ...] = ()
    delegation_depth: int = 0

    def __post_init__(self):
        from core.orchestration.contracts import nonempty, positive

        for key in ('task_id', 'step_id', 'base_revision'):
            nonempty(getattr(self, key), key)
        positive(self.plan_revision, 'plan_revision')
        positive(self.owner_generation, 'owner_generation')
        strings(self.evidence_refs, 'parent_evidence')
        if (type(self.permissions) is not Permissions or type(self.delegation_depth) is not int
                or self.delegation_depth < 0):
            raise ValueError('worker_parent_authority_invalid')


class Scheduler:
    def __init__(self, *, config, parent, sandbox, budget_pool, tools, provider, store, plans=None,
                 lease_seconds=30, workspaces=None, identity_check=None):
        if type(config) is not OrchestrationConfig or type(parent) is not ParentAuthority:
            raise ValueError('worker_scheduler_config_invalid')
        if type(sandbox) is not Permissions:
            raise ValueError('worker_sandbox_contract_invalid')
        self.config, self.parent, self.sandbox = config, parent, sandbox
        self.budget_pool, self.tools, self.provider, self.store = budget_pool, tools, provider, store
        self._lock = RLock()
        self._active = set()
        self._seen = set()
        self.plans = plans
        self.lease_seconds = lease_seconds
        self.workspaces = workspaces
        self.identity_check = identity_check

    def _validate(self, envelope, profile):
        if self.identity_check is not None:
            self.identity_check(envelope, profile)
        if not self.config.enabled:
            raise ValueError('worker_orchestration_disabled')
        if self.workspaces is not None and self.workspaces.state()['frozen']:
            raise ValueError('workspace_collaboration_frozen')
        parent = self.parent
        for key in ('task_id', 'base_revision', 'plan_revision', 'owner_generation'):
            if getattr(envelope, key) != getattr(parent, key):
                raise ValueError('worker_parent_version_mismatch')
        if envelope.parent_step_id != parent.step_id or envelope.step_id == parent.step_id:
            raise ValueError('worker_parent_step_mismatch')
        if envelope.depends_on and self.plans is None:
            raise ValueError('worker_dag_requires_persistent_plan')
        if self.plans is not None and self.workspaces is None and any(self.tools.registry.get(name) is None
                or self.tools.registry.get(name).effect != 'read' for name in envelope.allowed_tools):
            raise ValueError('worker_readonly_tool_refused')
        if (envelope.delegation_depth != parent.delegation_depth + 1
                or envelope.delegation_depth > min(self.config.max_depth, profile.delegation_depth)):
            raise ValueError('worker_depth_exceeded')
        if not envelope.budget.fits(profile.budget):
            raise ValueError('worker_role_budget_exceeded')
        needed = set(profile.model_capabilities) | ({'tools'} if envelope.allowed_tools else set())
        if any(name not in self.provider.capabilities
               or self.provider.capabilities[name].value is not True for name in needed):
            raise ValueError('worker_model_capability_unavailable')
        effective = intersect(envelope, profile, parent.permissions, self.sandbox, self.tools.registry)
        self._validate_memory(envelope, parent, effective)
        return effective

    def _validate_memory(self, envelope, parent, effective):
        selected = self.store.select(envelope, parent.evidence_refs, effective.memory_scope)
        self.store.validate_shared(selected, self.plans.tasks.store if self.plans else None)

    def _admit(self, plan, profile, step_id, accept, emit):
        try:
            return self._admit_checked(plan, profile, step_id, accept, emit)
        except ValueError as exc:
            if 'budget_exhausted' in str(exc) and self.config.budget_topup.enabled and self.plans is not None:
                from core.budget_topup.service import text_report
                value = self.budget_pool.bind(self.plans).budget_topup().status()
                raise ValueError(str(exc) + '；预算已耗尽或不足。\n' + text_report(value)) from exc
            raise

    def _admit_checked(self, plan, profile, step_id, accept, emit):
        if type(plan) is not PlanRevision or type(profile) is not WorkerProfile:
            raise ValueError('worker_plan_or_profile_invalid')
        with self._lock:
            from core.orchestration.plans import validate_plan
            validate_plan(plan)
            if plan.task_id != self.parent.task_id or plan.revision != self.parent.plan_revision:
                raise ValueError('worker_plan_stale')
            for step in plan.steps:
                self._validate(step, profile)
            envelope = next((s for s in plan.steps if s.step_id == step_id), None)
            if envelope is None:
                raise ValueError('worker_step_not_in_plan')
            key = (plan.task_id, plan.revision, step_id)
            if self.plans is None and (key in self._seen or len(self._active) >= self.config.max_workers):
                raise ValueError('worker_duplicate_or_capacity_exceeded')
            effective = self._validate(envelope, profile)
            lease = None
            if self.plans is not None:
                from uuid import uuid4
                lease = self.budget_pool.bind(self.plans, ttl=self.lease_seconds).claim(
                    plan, step_id, owner=uuid4().hex)
                key = (*key, lease.token.lease_id)
                reservation = None
            else:
                self.budget_pool.local_quota(self.config)
                reservation = self.budget_pool.reserve(envelope.budget)
            self._active.add(key)
            self._seen.add(key)
            authority = self.parent

        from core.orchestration.authority import guard
        revalidate = guard(self, lease, envelope, profile, effective, authority, reservation)

        def settle(result, *, unstarted=False):
            with self._lock:
                if lease is not None:
                    lease.settle_runtime(result, unstarted=unstarted)
                elif unstarted:
                    self.budget_pool.settle(reservation, calls=0, estimated_tokens=0)
                else:
                    known = result is not None and (result.usage_status == 'reported' or result.model_calls == 0)
                    self.budget_pool.settle(reservation, calls=result.model_calls if known else None,
                                            estimated_tokens=result.budget_summary.get('estimated_tokens') if known else None)
                self._active.remove(key)

        if lease is not None:
            revalidate.commit_fence = lease.commit_fence
            revalidate.quota = lease.quota
        else:
            revalidate.quota = self.budget_pool.local_quota(self.config).lease(reservation)
        factory = ReActWorker
        extra = {}
        if envelope.write_scope:
            if self.workspaces is None:
                settle(None, unstarted=True)
                raise ValueError('worker_write_requires_isolated_workspace')
            from core.orchestration.workspace_worker import WorkspaceWorker
            factory, extra = WorkspaceWorker, {'manager': self.workspaces}
        worker = factory(envelope, provider=self.provider, parent_tools=self.tools,
                           store=self.store, allowed_refs=self.parent.evidence_refs, effective=effective,
                           revalidate=revalidate, settle=settle, accept=accept, emit=emit, **extra)
        worker._budget_reservation = reservation
        worker.quota = revalidate.quota
        if self.plans is not None:
            from core.orchestration.plan_worker import PlanWorker
            worker = PlanWorker(worker, self.plans, lease=lease)
        return worker, envelope

    def ready(self):
        if not self.config.enabled:
            raise ValueError('worker_orchestration_disabled')
        if self.plans is None:
            raise ValueError('worker_dag_requires_persistent_plan')
        return self.plans.ready()

    async def dispatch_next(self, profile, *, accept=None, emit=None):
        """Default queue policy: critical path/user priority plus unbounded waiting age."""
        with self._lock:
            ready = self.ready()
            if not ready:
                raise ValueError('dag_no_ready_step')
            worker, envelope = self._admit(self.plans.current(), profile, ready[0], accept, emit)
        return await worker.run(envelope)

    async def dispatch(self, plan, profile, step_id, *, accept=None, emit=None):
        """Only trusted host calls this; merely constructing a Lead proposal does nothing."""
        worker, envelope = self._admit(plan, profile, step_id, accept, emit)
        return await worker.run(envelope)

    def admit(self, plan, profile, step_id, *, accept=None, emit=None):
        """B43 扇出准入（同步、按调用序确定性；返回一次性 Worker 交由监督器驱动）。

        与 dispatch 同校验链（父版本/权限交集/预算预留/深度/容量/去重），
        只是不把 run 包进单个 await——并发由 ChildSupervisor 管理。"""
        return self._admit(plan, profile, step_id, accept, emit)
