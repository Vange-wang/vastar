"""Host-only lease capability; generations are not supplied by the model."""
import asyncio


class LeaseHandle:
    def __init__(self, service, token):
        self.service, self.token = service, token
        from core.orchestration.quota_runtime import RuntimeQuota
        self.quota = RuntimeQuota(self)

    def detach(self, owner):
        from core.orchestration.detached import transfer
        return transfer(self, owner)

    def check(self, *, allow_settled=False):
        return self.service.check(self.token, allow_settled=allow_settled)

    def commit_fence(self):
        return self.service.fence(self.token)

    async def maintain(self):
        while True:
            await asyncio.sleep(self.service.ttl / 3)
            self.service.heartbeat(self.token)

    def started(self):
        self.service.started(self.token)

    def settle_runtime(self, result, *, unstarted=False):
        unstarted = unstarted or (result is not None and result.model_calls == 0)
        known = result is not None and result.usage_status == 'reported'
        calls = 0 if unstarted else result.model_calls if known else None
        tokens = 0 if unstarted else result.budget_summary.get('estimated_tokens') if known else None
        with self.service.transaction():
            row = self.service.repo.get(self.token.lease_id)
            # 已被宿主结算的关闭态租约（expired=核账回收 / cancelled=管理取消结清）不得再扣：
            # 管理取消（management.cancel）先全额结清在途租约，运行时取消收口随后到达，
            # 二次结算会误报 worker_reservation_already_settled（B51 真并发演练首贯）。
            if row['state'] in ('expired', 'cancelled') and row['settled']:
                return  # Recovery/management already charged this generation, never twice.
            self.service.settle(self.token, calls=calls, estimated_tokens=tokens)

    def record(self, envelope, result):
        with self.service.transaction():
            if self.service.quotas.row(self.token.lease_id)['detached']:
                from core.orchestration.detached import record
                return record(self, envelope, result)
            row = self.check(allow_settled=True)
            if not row['settled']:
                raise ValueError('worker_settlement_required')
            self.service.plans.record(envelope, result, lease=self)
            self.service.repo.close(self.token.lease_id, 'finished')
