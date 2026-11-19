"""Reserve the existing Task account for one host run; settle from durable child facts."""
from types import SimpleNamespace
from uuid import uuid4

from core.harness.task_commands import _source
from core.harness.task_runtime import TaskRuntime
from core.orchestration.contracts import Budget


def begin(coord, config, raw):
    store = coord._store
    task = coord.task_service.current(coord.session_id)
    run_id = 'team-' + uuid4().hex
    message = _source(coord, '/team run ' + raw)
    store.create_run(run_id, coord.session_id, message, config_snapshot={'entry': 'team'})
    store.task_link_run(coord.session_id, run_id)
    try:
        step_id, calls, tokens = TaskRuntime(coord.task_service).reserve(coord.session_id, run_id)
        host_calls = coord._token_cfg.get('run_max_calls')
        host_tokens = coord._token_cfg.get('run_max_estimated_tokens')
        calls = min(calls, host_calls) if host_calls is not None else calls
        tokens = min(t for t in (tokens, host_tokens, 40000) if t is not None)
        deadline = max(1, int(min(60, coord._primary.timeouts.get('run_total_s') or 60)))
        from core.orchestration.entry_budget import remaining
        money, seconds = remaining(coord, task, config)
        limit = Budget(calls, tokens, deadline, money, seconds)
        step = next(s for s in task['steps'] if s['step_id'] == step_id)
        return task, step, run_id, limit
    except BaseException:
        finish(coord, run_id, None, {'status': 'incomplete', 'reason': 'team_admission_failed'})
        raise


def finish(coord, run_id, scheduler, result):
    calls = tokens = 0
    if scheduler is not None and scheduler.budget_pool._persistent is not None:
        service = scheduler.budget_pool._persistent
        account = service.repo.account()
        available = service.available
        calls = max(account['spent_calls'], scheduler.budget_pool.limit.max_calls - available[0])
        tokens = max(account['spent_tokens'], scheduler.budget_pool.limit.max_estimated_tokens - available[1])
    task = coord.task_service.current(coord.session_id)
    usage = SimpleNamespace(run_id=run_id, model_calls=calls, budget_summary={'estimated_tokens': tokens},
                            usage_totals={}, cost_summary={}, call_records=[])
    with coord._store.transaction():
        if scheduler is not None and scheduler.budget_pool._persistent is not None:
            from core.orchestration.entry_budget import settle_dimensions
            settle_dimensions(coord, task['task_id'], run_id, scheduler.budget_pool._persistent)
        TaskRuntime(coord.task_service).settle(task['task_id'], usage)
    status = 'COMPLETED' if result['status'] == 'integrated' else 'INCOMPLETE'
    coord._store.finalize_run(run_id, status, result=result, durability='confirmed', latencies={})


def identity_guard(coord, task, run_id):
    def check(envelope, profile):
        current = coord.task_service.current(coord.session_id)
        coord._store._owner.assert_held()
        row = TaskRuntime(coord.task_service).snapshot(task['task_id'])
        if (current['task_id'] != task['task_id'] or current['revision'] != task['revision']
                or current['status'] in {'COMPLETED', 'CANCELLED', 'BLOCKED', 'WAITING_INPUT'}
                or row['active_run'] != run_id):
            raise ValueError('team_parent_authority_changed')
    return check
