"""Shared user-only authorization and progress presentation for both accounts."""
from memory.repositories.budget_topups import TopupRepository


def command(account_id, challenge):
    return f'/task budget approve {account_id} {challenge}'


def guard(tasks, session_id):
    task = tasks.current(session_id)
    if task['status'] in {'CANCELLED', 'COMPLETED', 'BLOCKED'}:
        raise ValueError('budget_task_not_executable')
    db = tasks.store._conn
    row = db.execute('SELECT active_run,recovery_error FROM task_runtime WHERE task_id=?',
                     (task['task_id'],)).fetchone()
    if row and (row['active_run'] or row['recovery_error']):
        raise ValueError('budget_requires_run_reconciliation')
    if db.execute("SELECT 1 FROM tool_operations WHERE task_id=? AND state IN ('prepared','executing','unknown')",
                  (task['task_id'],)).fetchone():
        raise ValueError('budget_requires_operation_reconciliation')
    return task


def authorize(adapter, message_id, challenge):
    if not adapter.config.enabled:
        raise ValueError('budget_topup_disabled')
    with adapter.transaction():
        adapter.check()
        task = guard(adapter.tasks, adapter.session_id)
        source = adapter.tasks._source(adapter.session_id, message_id)
        source['origin'] = adapter.store._conn.execute('SELECT origin FROM messages WHERE message_id=?',
                                                       (message_id,)).fetchone()[0]
        if source['origin'] not in {'user', 'test'}:
            raise ValueError('budget_user_origin_required')
        if source['text'].strip() != command(adapter.account_id, challenge):
            raise ValueError('budget_explicit_authorization_required')
        source['task_id'] = task['task_id']
        repo = adapter.repository()
        return repo.increase(adapter.account_id, challenge, message_id, source,
                             adapter.balance(), adapter.apply, adapter.balance)


def report(adapter):
    from decimal import Decimal
    with adapter.transaction():
        repo = adapter.repository()
        row = repo.account(adapter.account_id)
        balance = adapter.balance()
        task = adapter.tasks.current(adapter.session_id)
        completed, remaining = adapter.progress(task)
        exhausted = any(balance[k] < 1 for k in ('calls', 'tokens')) or any(
            balance[k] <= 0 for k in ('cost_yuan', 'active_seconds'))
        entry = command(adapter.account_id, row['challenge']) if adapter.config.enabled else '预算续加已关闭'
        return dict(status='预算已耗尽' if exhausted else '预算状态（不足以启动下一项时也须申请续加）',
                    account_id=adapter.account_id, initial=row['initial'], total_credit=row['total'], balance=balance,
                    next_added={k: str(Decimal(v) * Decimal(row['ratio'])) for k, v in row['initial'].items()},
                    grant_count=row['ordinal'], completed=completed, remaining=remaining,
                    blockers=adapter.blockers(), next_action=entry,
                    note='每次明确授权仅新增初始预算的固定比例；不会自动执行或自动再加。调用/token整单位使用，零头累计。',
                    audit=repo.history(adapter.account_id))


def text_report(value):
    import json
    return (value['status'] + '\n已完成：' + json.dumps(value['completed'], ensure_ascii=False)
            + '\n未完成：' + json.dumps(value['remaining'], ensure_ascii=False)
            + '\n阻塞：' + json.dumps(value['blockers'], ensure_ascii=False)
            + '\n余额：' + json.dumps(value['balance'], ensure_ascii=False)
            + '\n初始预算：' + json.dumps(value['initial'], ensure_ascii=False)
            + f"\n第 {value['grant_count'] + 1} 次授权将新增：" + json.dumps(value['next_added'], ensure_ascii=False)
            + '\n逐次授权入口：' + value['next_action'] + '\n' + value['note'])


def repository(adapter, initial, mode):
    repo = TopupRepository(adapter.store)
    repo.ensure(adapter.account_id, adapter.task_id, mode, initial, adapter.config.ratio)
    return repo
