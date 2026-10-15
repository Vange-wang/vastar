"""Persist terminal facts only after B42 has drained operations and settled its slot."""
import asyncio

from core.orchestration.validation import terminal


class PlanWorker:
    def __init__(self, worker, plans, *, lease=None):
        self.worker, self.plans = worker, plans
        self.lease = lease
        self.quota = getattr(worker, 'quota', None)
        self.identity = worker.identity
        self._used = False

    def _record(self, envelope, result):
        if self.lease is not None:
            self.lease.record(envelope, result)
        else:
            self.plans.record(envelope, result)

    def cancel_before_start(self, envelope):
        if self._used:
            return None
        result = self.worker.cancel_before_start(envelope)
        if result is not None:
            self._used = True
            self._record(envelope, result)
        return result

    async def run(self, envelope):
        if self._used:
            raise ValueError('worker_admission_mismatch_or_consumed')
        self._used = True
        heartbeat = None
        try:
            if self.lease is not None:
                try:
                    self.lease.started()
                except ValueError:
                    self.worker.cancel_before_start(envelope)
                    raise
                heartbeat = asyncio.create_task(self.lease.maintain())
            result = await self.worker.run(envelope)
        except asyncio.CancelledError:
            self._record(envelope, terminal(envelope, 'cancelled', 'worker_parent_cancelled'))
            raise
        except Exception:
            self._record(envelope, terminal(envelope, 'failed', 'worker_host_failure'))
            raise
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
        self._record(envelope, result)
        return result
