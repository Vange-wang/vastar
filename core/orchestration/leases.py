"""One transaction owns DAG admission, expiring rights, resource locks and quota."""
import math
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import uuid4

from core.orchestration.contracts import Budget, nonempty, positive, strings
from core.orchestration.plans import decode_plan, ready_order
from memory.repositories.worker_leases import WorkerLeaseRepository


@dataclass(frozen=True)
class LeaseToken:
    lease_id: str
    task_id: str
    step_id: str
    revision: int
    generation: int
    owner: str

    def __post_init__(self):
        for key in ('lease_id', 'task_id', 'step_id', 'owner'):
            nonempty(getattr(self, key), key)
        positive(self.revision, 'lease_revision')
        positive(self.generation, 'lease_generation')


class LeaseService:
    def __init__(self, plans, limit, *, ttl=30):
        plans._guard()  # Disabled means no extension DDL and no provider access.
        if type(limit) is not Budget:
            raise ValueError('worker_parent_budget_invalid')
        if type(ttl) not in (int, float) or not math.isfinite(ttl) or ttl <= 0:
            raise ValueError('worker_lease_ttl_invalid')
        self.plans, self.limit, self.ttl = plans, limit, ttl
        self.store, self.clock = plans.tasks.store, plans.clock
        self.repo = WorkerLeaseRepository(self.store, plans.task_id, limit)
        from memory.repositories.worker_quotas import WorkerQuotaRepository
        with self.store.transaction():
            self.quotas = WorkerQuotaRepository(self.store, plans.task_id, plans.config, limit)
            unmapped = self.store._conn.execute('SELECT 1 FROM worker_leases l WHERE task_id=? '
                'AND NOT EXISTS (SELECT 1 FROM worker_quota_reservations q WHERE q.lease_id=l.lease_id)',
                (plans.task_id,)).fetchone()
            if unmapped:
                raise ValueError('worker_legacy_quota_reconciliation_required')
            from core.orchestration.dispatch_gate import ensure
            ensure(self.store._conn)

    @contextmanager
    def transaction(self):
        with self.store.transaction():
            if getattr(self.store, '_worker_commit_fence', False):
                raise ValueError('worker_commit_reentrant_mutation')
            yield

    @property
    def available(self):
        with self.store.transaction():
            return self.repo.available()

    def budget_topup(self):
        from core.budget_topup.worker import WorkerTopup
        if not self.plans.config.budget_topup.enabled:
            raise ValueError('budget_topup_disabled')
        return WorkerTopup(self, self.plans.config.budget_topup)

    def _reap(self, value):
        for prior in self.repo.expired(self.clock()):
            if not prior['settled']:
                zero = 0 if not prior['started'] else None
                self.repo.settle(prior['lease_id'], zero, zero)
                self.quotas.settle(prior['lease_id'], self.clock(), unstarted=zero == 0, unknown=zero is None)
            self.repo.close(prior['lease_id'], 'expired')
            row = value['records'].get(prior['step_id'])
            if row and row.get('lease_id') == prior['lease_id'] and row['state'] == 'running':
                row.update(state='queued', result=None, lease_id=None)

    def recover_expired(self):
        """Explicit host recovery, not restart/run resumption or a B46 cancel tree."""
        with self.transaction():
            value = self.plans.snapshot()
            self._reap(value)
            self.plans.repo.write(self.plans.task_id, value, value['plan']['revision'])

    def claim(self, plan, step_id, *, owner, resources=()):
        nonempty(owner, 'lease_owner')
        strings(resources, 'lease_resources')
        if len(resources) != len(set(resources)):
            raise ValueError('worker_resource_duplicate')
        with self.transaction():
            from core.orchestration.dispatch_gate import check
            check(self)
            value = self.plans._load(plan.revision)
            if decode_plan(value['plan']) != plan:
                raise ValueError('dag_plan_content_mismatch')
            self._reap(value)
            if step_id not in ready_order(plan, value['records'], self.clock()):
                raise ValueError('dag_step_not_ready_or_accepted')
            if sum(r['state'] == 'running' for r in value['records'].values()) >= self.plans.config.max_workers:
                raise ValueError('dag_capacity_exceeded')
            if not self.repo.locks_available(resources):
                raise ValueError('worker_resource_locked')
            step = next(s for s in plan.steps if s.step_id == step_id)
            calls, tokens = self.repo.available()
            if (not step.budget.fits(self.limit) or step.budget.max_calls > calls
                    or step.budget.max_estimated_tokens > tokens):
                raise ValueError('worker_parent_budget_exhausted')
            token = LeaseToken(uuid4().hex, plan.task_id, step_id, plan.revision,
                               self.repo.generation(step_id), owner)
            self.quotas.reserve(token, step.budget, self.limit)
            self.repo.insert(token, step.budget, self.clock() + self.ttl, resources)
            row = value['records'][step_id]
            row.update(state='running', attempts=row['attempts'] + 1, lease_id=token.lease_id)
            self.plans.repo.write(plan.task_id, value, plan.revision)
        from core.orchestration.lease_handle import LeaseHandle
        return LeaseHandle(self, token)

    def check(self, token, *, allow_settled=False):
        if type(token) is not LeaseToken:
            raise ValueError('worker_lease_token_mismatch')
        with self.store.transaction():
            row = self.repo.get(token.lease_id)
            independent = self.quotas.row(token.lease_id)['detached']
            value = self.plans.repo.read(token.task_id) if independent else self.plans._load(token.revision)
            record = value['records'].get(token.step_id, {})
            if (token.task_id != self.plans.task_id
                    or any(row[k] != getattr(token, k) for k in
                           ('task_id', 'step_id', 'revision', 'generation', 'owner'))
                    or row['state'] != 'active' or row['expires'] <= self.clock()
                    or (row['settled'] and not allow_settled)
                    or record.get('lease_id') != token.lease_id or record.get('state') != 'running'):
                raise ValueError('worker_lease_lost_or_expired')
            return row

    def heartbeat(self, token):
        with self.transaction():
            self.check(token)
            self.store._conn.execute('UPDATE worker_leases SET expires=? WHERE lease_id=?',
                                     (self.clock() + self.ttl, token.lease_id))

    def started(self, token):
        with self.transaction():
            from core.orchestration.dispatch_gate import check
            check(self, token.lease_id)
            self.check(token)
            self.store._conn.execute('UPDATE worker_leases SET started=1 WHERE lease_id=?', (token.lease_id,))

    def settle(self, token, *, calls=None, estimated_tokens=None):
        with self.transaction():
            # Late settlement cannot affect another generation's reservation.
            row = self.repo.get(token.lease_id)
            if type(token) is not LeaseToken or any(row[k] != getattr(token, k) for k in
                    ('task_id', 'step_id', 'revision', 'generation', 'owner')):
                raise ValueError('worker_lease_token_mismatch')
            self.repo.settle(token.lease_id, calls, estimated_tokens)
            self.quotas.settle(token.lease_id, self.clock(), unstarted=calls == estimated_tokens == 0,
                               unknown=calls is None or estimated_tokens is None)

    @contextmanager
    def fence(self, token):
        """Serialize bounded local commit with claims, INCLUDING the filesystem syscall.

        An admitted syscall cannot be undone on timeout. Claims wait for it to drain.
        This is not a fence for a remote service, subprocess or another filesystem writer.
        """
        with self.store.transaction():
            self.check(token)
            prior = getattr(self.store, '_worker_commit_fence', None)
            if prior is not None and prior != token.lease_id:
                raise ValueError('worker_commit_reentrant_mutation')
            self.store._worker_commit_fence = token.lease_id
            try:
                yield
            finally:
                self.store._worker_commit_fence = prior
