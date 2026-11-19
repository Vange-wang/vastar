"""Bridge existing parent money/time accounts without granting a second allowance."""
from core.harness.task_runtime import TaskRuntime


def single_account(store, task_id):
    if not store._conn.execute("SELECT 1 FROM sqlite_master WHERE name='worker_quota_accounts'").fetchone():
        return None
    return store._conn.execute('SELECT * FROM worker_quota_accounts WHERE account_id=?',
                               ('single:' + task_id,)).fetchone()


def remaining(coord, task, config):
    store = coord._store
    account = single_account(store, task['task_id'])
    money = task['budget'].get('max_cost_yuan', config.max_cost_yuan)
    seconds = task['budget'].get('max_active_seconds', config.max_active_seconds)
    if account is not None:
        if store._conn.execute('SELECT 1 FROM worker_quota_reservations WHERE account_id=? AND settled=0',
                                (account['account_id'],)).fetchone():
            raise ValueError('team_parent_quota_reconciliation_required')
        money = min(money, (account['cost'] - account['spent_cost']) / 1000000)
        seconds = min(seconds, account['seconds'] - account['spent_seconds'])
    elif (TaskRuntime(coord.task_service).snapshot(task['task_id'])['used_calls']
          and any(k in task['budget'] for k in ('max_cost_yuan', 'max_active_seconds'))):
        raise ValueError('team_prior_money_time_unmeasured: inspect /task budget before delegation')
    if money <= 0 or seconds <= 0:
        raise ValueError('team_parent_budget_exhausted:money_or_active_time')
    return min(money, config.max_cost_yuan), min(seconds, config.max_active_seconds)


def settle_dimensions(coord, task_id, run_id, service):
    """Parent reservation stays held until child aggregate is accounted atomically."""
    store = coord._store
    with store.transaction():
        active = TaskRuntime(coord.task_service).snapshot(task_id)['active_run']
        if active != run_id or single_account(store, task_id) is None:
            return
        account = service.quotas.account()
        store._conn.execute('UPDATE worker_quota_accounts SET spent_cost=spent_cost+?, '
                            'spent_seconds=spent_seconds+? WHERE account_id=?',
                            (account['spent_cost'], account['spent_seconds'], 'single:' + task_id))
