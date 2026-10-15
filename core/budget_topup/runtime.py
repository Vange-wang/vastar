"""Single-Agent admission, priced events, and active runtime integration."""
from dataclasses import replace
from types import SimpleNamespace

from core.budget_topup.commands import config
from core.budget_topup.single import SingleTopup
from core.harness.task_runtime import TaskRuntime
from core.orchestration.contracts import Budget
from core.orchestration.quota_runtime import RuntimeQuota
from memory.repositories.budget_topups import exists


def reserve_task(coord, request):
    tasks = coord.task_service
    with tasks.store.transaction():
        task = tasks.current(request.session_id)
        cfg = config(coord)
        enrolled = exists(tasks.store, 'single:' + task['task_id'])
        explicit = all(k in task['budget'] for k in ('max_estimated_tokens', 'max_cost_yuan', 'max_active_seconds'))
        account = None
        if enrolled or cfg.enabled and explicit:
            account = SingleTopup(tasks, request.session_id, cfg)
            account.repository()
        result = TaskRuntime(tasks).reserve(request.session_id, request.run_id)
        if account is not None:
            request.budget_quota = reserve_quota(account, request, result)
        return result


def reserve_quota(account, request, result):
    balance = account.quotas.balance()
    if any(v <= 0 for v in balance.values()):
        raise ValueError('预算已耗尽；/task budget 查看进度并逐次授权续加')
    budget = Budget(result[1], result[2], max_cost_yuan=balance['cost_yuan'],
                    max_active_seconds=balance['active_seconds'])
    key = request.run_id
    account.quotas.reserve(SimpleNamespace(lease_id=key), budget, account.limit)
    account.plans = SimpleNamespace(task_id=account.account_id)

    def check():
        task = account.tasks.current(account.session_id)
        row = TaskRuntime(account.tasks).snapshot(account.task_id)
        if (task['task_id'] != account.task_id or task['status'] in {'CANCELLED', 'COMPLETED', 'BLOCKED'}
                or row['active_run'] != key or row['recovery_error']):
            raise ValueError('budget_run_authority_changed')

    quota = RuntimeQuota(SimpleNamespace(service=account, token=SimpleNamespace(lease_id=key), check=check))
    quota.allow_price_changes = True  # Single-Agent fallback/compact models have their own pinned call price.
    return quota


def exhaustion_note(coord, error):
    if '预算' not in error and 'budget' not in error.lower():
        return error
    from core.budget_topup.service import text_report
    if not config(coord).enabled:
        return error + '；预算续加已关闭'
    account = SingleTopup(coord.task_service, coord.session_id, config(coord))
    return '预算已耗尽或不足以启动下一项。\n' + text_report(account.status())


async def run_budgeted(coord, request):
    quota = getattr(request, 'budget_quota', None)
    if quota is None:
        return await coord._runtime.run(request, coord._emit_checked)
    from core.budget_topup.tool_runtime import CURRENT
    from core.orchestration.worker_runtime import _authority_guard
    old_guard = request.memory_guard

    def guard():
        if old_guard:
            old_guard()
        quota.check()

    request.memory_guard = _authority_guard(guard)
    for key in ('primary', 'fallback'):
        spec = getattr(request, key)
        if spec is not None:
            seconds = quota.remaining_seconds()
            timeouts = {**spec.timeouts}
            for name in ('attempt_total_s', 'first_delta_s', 'stream_idle_s', 'connect_s'):
                from core.limits import cap_timeout
                timeouts[name] = cap_timeout(timeouts.get(name, seconds), seconds)
            setattr(request, key, replace(spec, timeouts=timeouts))
    async def emit(event):
        quota.event(event)
        await coord._emit_checked(event)
    token = CURRENT.set(quota)
    result = None
    try:
        result = await coord._runtime.run(request, emit)
        return result
    finally:
        try:
            tools = coord._runtime._tools
            if tools is not None:
                await tools.operations.close()
            with quota.service.transaction():
                quota.repo.settle(quota.key, quota.service.clock(), unknown=result is None)
        finally:
            CURRENT.reset(token)


def notify_exhausted(coord, result):
    task = coord.task_service.current(coord.session_id)
    if not exists(coord._store, 'single:' + task['task_id']):
        return
    account = SingleTopup(coord.task_service, coord.session_id, config(coord))
    value = account.status()
    errors = str(result.errors) + str(getattr(result, 'warnings', []))
    if value['status'] == '预算已耗尽' or 'budget' in errors.lower() or 'BUDGET' in errors:
        from core.budget_topup.service import text_report
        value['status'] = '预算已耗尽或剩余额度不足以启动下一项'
        coord._renderer.render_note(text_report(value))


def model_timeout(request, spec):
    from core.limits import cap_timeout, optional_limit
    timeout = optional_limit(spec.timeouts.get('attempt_total_s', 120))
    quota = getattr(request, 'budget_quota', None)
    return cap_timeout(timeout, quota.remaining_seconds()) if quota else timeout
