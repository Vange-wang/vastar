"""Host-priced model admissions and measured active intervals (not parent waiting)."""
from contextlib import contextmanager
from decimal import Decimal
from uuid import uuid4

from core.errors import ErrorCategory, ErrorCode, VastarError
from memory.repositories.worker_quotas import micros


def quote(pricing, tokens):
    if pricing.get('currency') != 'CNY' or not pricing.get('version'):
        raise ValueError('worker_pricing_required:CNY_version')
    rates = [pricing.get(key) for key in ('input_per_million', 'output_per_million')]
    if any(type(v) not in (int, float) for v in rates):
        raise ValueError('worker_pricing_required:rates')
    for rate in rates:
        micros(rate)  # finite, nonnegative; never silently coerce invalid prices to free.
    return micros(sum(Decimal(str(r)) * Decimal(str(t)) / 1000000
                      for r, t in zip(rates, tokens, strict=True)))


class RuntimeQuota:
    def __init__(self, lease):
        self.lease = lease
        self.service = lease.service
        self.repo = self.service.quotas
        self.key = lease.token.lease_id
        self.pricing = None
        self.pricing_by_call = {}
        self.allow_price_changes = False

    def check(self):
        with self.service.transaction():
            from core.orchestration.dispatch_gate import check
            check(self.service, self.key)
            self.lease.check()
            self.repo.check(self.key, self.service.clock())

    def event(self, event):
        payload = event.payload
        overrun = None
        try:
            with self.service.transaction():
                if event.type == 'model_call.intent':
                    self.lease.check()
                    pricing = payload['contract']['pricing']
                    if not self.allow_price_changes and self.pricing is not None and pricing != self.pricing:
                        raise ValueError('worker_pricing_changed')
                    estimate = payload['estimated_usage']
                    cost = quote(pricing, (estimate['input_tokens'], estimate['components']['output_reserve']))
                    self.repo.begin(self.key, self.service.clock(), cost, activity_id=payload['call_id'])
                    self.pricing = dict(pricing)
                    self.pricing_by_call[payload['call_id']] = dict(pricing)
                elif event.type == 'model_call.finished':
                    cost = 0 if not payload.get('started') else None
                    if payload.get('started') and payload.get('usage_status') == 'reported':
                        values = payload['actual_usage']
                        cost = quote(self.pricing_by_call[payload['call_id']],
                                     (values['input_tokens'], values['output_tokens']))
                    self.repo.end(self.key, self.service.clock(), cost, activity_id=payload['call_id'])
                    row = self.repo.row(self.key)
                    if row['used_cost'] > row['cost'] or row['used_seconds'] > row['seconds']:
                        overrun = 'cost_yuan' if row['used_cost'] > row['cost'] else 'active_seconds'
            if overrun:
                raise ValueError(f'worker_parent_budget_exhausted:{overrun}:reported_overrun')
        except ValueError as exc:
            raise VastarError(ErrorCode.PARAM_ERROR, ErrorCategory.PARAM, str(exc)) from exc

    @contextmanager
    def tool(self):
        activity_id = 'tool-' + uuid4().hex
        with self.service.transaction():
            self.check()
            self.lease.check()
            self.repo.begin(self.key, self.service.clock(), activity_id=activity_id)
        try:
            yield
        finally:
            with self.service.transaction():
                self.repo.end(self.key, self.service.clock(), 0, activity_id=activity_id)

    def remaining_seconds(self):
        with self.service.transaction():
            row = self.repo.row(self.key)
            elapsed = 0 if row['active_since'] is None else self.service.clock() - row['active_since']
            return max(0, row['seconds'] - row['used_seconds'] - max(0, elapsed))

    def pause(self):
        with self.service.transaction():
            self.repo.pause(self.key, self.service.clock())
