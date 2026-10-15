"""Task-local active tool intervals; user permission waiting consumes no time."""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar

CURRENT = ContextVar('single_task_budget_quota', default=None)


@contextmanager
def user_wait():
    quota = CURRENT.get()
    if quota is None:
        yield
        return
    quota.pause()
    try:
        yield
    finally:
        with quota.service.transaction():
            row = quota.repo.row(quota.key)
            if not row['settled'] and row['active_since'] is None:
                quota.repo.db.execute('UPDATE worker_quota_reservations SET active_since=? WHERE lease_id=? '
                                     'AND EXISTS(SELECT 1 FROM worker_quota_activity WHERE lease_id=?)',
                                     (quota.service.clock(), quota.key, quota.key))


def attach_control(control):
    quota = CURRENT.get()
    if quota is None:
        return
    control.budget_check = quota.check


async def execute_tools(runner, s, calls, attempt_id, attempt_index, results):
    quota = CURRENT.get()
    if quota is None:
        return await runner._execute_tool_calls(s, calls, attempt_id, attempt_index, messages=results)
    async def watch():
        while True:
            await asyncio.sleep(0.01)
            try:
                quota.check()
            except ValueError:
                s.request.cancel_event.set()
                return
    with quota.tool():
        watcher = asyncio.create_task(watch())
        try:
            return await runner._execute_tool_calls(s, calls, attempt_id, attempt_index, messages=results)
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            await runner._tools.operations.close()
