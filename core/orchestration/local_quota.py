"""Same money/time ledger for the non-persistent B42/B43 host API, created on admit."""
import sqlite3
import time
from contextlib import contextmanager
from types import SimpleNamespace

from core.orchestration.dispatch_gate import ensure
from core.orchestration.quota_runtime import RuntimeQuota
from memory.repositories.worker_quotas import WorkerQuotaRepository


class LocalQuota:
    def __init__(self, pool, config):
        self.pool, self.config = pool, config
        self.clock = time.monotonic
        db = sqlite3.connect(':memory:', check_same_thread=False)
        db.row_factory = sqlite3.Row
        self.store = SimpleNamespace(_conn=db)
        self.plans = SimpleNamespace(task_id='local-parent')
        with self.transaction():
            ensure(db)
            self.quotas = WorkerQuotaRepository(self.store, self.plans.task_id, config, pool.limit)

    @contextmanager
    def transaction(self):
        with self.pool._lock, self.store._conn:
            yield

    def reserve(self, key, budget):
        with self.transaction():
            self.quotas.reserve(SimpleNamespace(lease_id=key), budget, self.pool.limit)

    def lease(self, key):
        host = self
        class Handle:
            service = host
            token = SimpleNamespace(lease_id=key)

            def check(self):
                if key not in host.pool._held and key not in host.pool._detached:
                    raise ValueError('worker_reservation_already_settled')
        return RuntimeQuota(Handle())

    def settle(self, key, calls, tokens):
        with self.transaction():
            self.quotas.settle(key, self.clock(), unstarted=calls == tokens == 0,
                               unknown=calls is None or tokens is None)

    def detach(self, key, budget, owner):
        with self.transaction():
            self.quotas.transfer(dict(lease_id=key, calls=budget.max_calls,
                                      tokens=budget.max_estimated_tokens), owner)
