"""In-process shared parent account. Unknown usage retains the full reservation."""
from threading import RLock
from uuid import uuid4

from core.orchestration.contracts import Budget


class BudgetPool:
    def __init__(self, budget: Budget):
        if type(budget) is not Budget:
            raise ValueError('worker_parent_budget_invalid')
        self.limit = budget
        self._lock = RLock()
        self._held = {}
        self._spent = [0, 0]
        self._persistent = None
        self._granted = [0, 0]
        self._detached = {}
        self._local = None

    def local_quota(self, config):
        from core.orchestration.local_quota import LocalQuota
        with self._lock:
            if self._local is None:
                self._local = LocalQuota(self, config)
            elif self._local.config != config:
                raise ValueError('worker_parent_budget_changed')
            return self._local

    def bind(self, plans, *, ttl=30):
        """A persisted DAG always uses its durable shared account, never a second pool."""
        from core.orchestration.leases import LeaseService

        with self._lock:
            if self._persistent is None:
                if self._held or any(self._spent) or self._local is not None or any(self._granted):
                    raise ValueError('worker_budget_already_used_in_memory')
                self._persistent = LeaseService(plans, self.limit, ttl=ttl)
            elif self._persistent.plans is not plans:
                raise ValueError('worker_budget_binding_changed')
            return self._persistent

    @property
    def available(self):
        with self._lock:
            if self._persistent is not None:
                return self._persistent.available
            return (self.limit.max_calls - self._spent[0] - self._granted[0] - sum(b.max_calls for b in self._held.values()),
                    self.limit.max_estimated_tokens - self._spent[1]
                    - self._granted[1] - sum(b.max_estimated_tokens for b in self._held.values()))

    def detach_worker(self, worker, owner):
        with self._lock:
            key = worker._budget_reservation
            if key in self._detached or key not in self._held:
                raise ValueError('worker_detach_already_transferred_or_settled')
            budget = self._held[key]
            if self._local is not None:
                self._local.detach(key, budget, owner)
            del self._held[key]
            child = BudgetPool(budget)
            child.owner = owner
            child._held[key] = budget
            self._granted[0] += budget.max_calls
            self._granted[1] += budget.max_estimated_tokens
            self._detached[key] = child

    def reserve(self, budget):
        with self._lock:
            if self._persistent is not None:
                raise ValueError('worker_budget_requires_atomic_claim')
            calls, tokens = self.available
            if (type(budget) is not Budget or not budget.fits(self.limit)
                    or budget.max_calls > calls or budget.max_estimated_tokens > tokens):
                raise ValueError('worker_parent_budget_exhausted')
            key = uuid4().hex
            if self._local is not None:
                self._local.reserve(key, budget)
            self._held[key] = budget
            return key

    def settle(self, key, *, calls=None, estimated_tokens=None):
        with self._lock:
            if self._local is not None:
                self._local.settle(key, calls, estimated_tokens)
            if key in self._detached:
                return self._detached[key].settle(key, calls=calls, estimated_tokens=estimated_tokens)
            budget = self._held.get(key)
            if budget is None:
                raise ValueError('worker_reservation_already_settled')
            values = (calls, estimated_tokens)
            limits = (budget.max_calls, budget.max_estimated_tokens)
            if any(v is not None and (type(v) is not int or v < 0) for v in values):
                raise ValueError('worker_usage_invalid')
            # Unknown retains quota; an unexpected overshoot becomes debt, never free credit.
            spent = limits if None in values else values
            self._spent = [old + used for old, used in zip(self._spent, spent, strict=True)]
            del self._held[key]
