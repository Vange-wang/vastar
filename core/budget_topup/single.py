"""Opt-in durable four-dimensional extension of the legacy Task account.

Legacy execution stays intact until budget inspection/enrolment. Prior unmeasured
money/time is conservatively full, never invented as zero consumption.
"""
import json
import time
from types import SimpleNamespace

from core.budget_topup.service import authorize, report, repository
from core.harness.task_runtime import TaskRuntime
from core.orchestration.contracts import Budget
from memory.repositories.budget_topups import exists, spendable
from memory.repositories.worker_quotas import WorkerQuotaRepository, micros


class SingleTopup:
    def __init__(self, tasks, session_id, config, *, clock=time.monotonic):
        self.tasks, self.session_id, self.config = tasks, session_id, config
        self.store, self.clock = tasks.store, clock
        self.task_id = tasks.current(session_id)['task_id']
        self.account_id = 'single:' + self.task_id
        self.transaction = self.store.transaction

    def repository(self):
        task = self.tasks.current(self.session_id)
        budget = task['budget']
        initial = dict(calls=budget['max_calls'], tokens=budget.get('max_estimated_tokens',
                       self.config.legacy_max_estimated_tokens), cost_yuan=budget.get('max_cost_yuan',
                       self.config.legacy_max_cost_yuan), active_seconds=budget.get('max_active_seconds',
                       self.config.legacy_max_active_seconds))
        self.limit = Budget(initial['calls'], initial['tokens'], max_cost_yuan=initial['cost_yuan'],
                            max_active_seconds=initial['active_seconds'])
        prior = exists(self.store, self.account_id)
        repo = repository(self, initial, 'single')
        cfg = SimpleNamespace(max_cost_yuan=initial['cost_yuan'], max_active_seconds=initial['active_seconds'],
                              budget_policy_version=1)
        self.quotas = WorkerQuotaRepository(self.store, self.account_id, cfg, self.limit)
        from core.orchestration.dispatch_gate import ensure
        ensure(self.store._conn)
        if not prior and TaskRuntime(self.tasks).snapshot(self.task_id)['used_calls']:
            self.store._conn.execute('UPDATE worker_quota_accounts SET spent_cost=cost,spent_seconds=seconds '
                                    'WHERE account_id=?', (self.account_id,))
        return repo

    def balance(self):
        runtime = TaskRuntime(self.tasks).snapshot(self.task_id)
        account = self.quotas.account()
        return dict(calls=account['calls'] - runtime['used_calls'] - runtime['reserved_calls'],
                    tokens=account['tokens'] - runtime['estimated_tokens'], **self.quotas.balance())

    def check(self):
        if self.store._conn.execute("SELECT 1 FROM sqlite_master WHERE name='worker_quota_reservations'").fetchone():
            if self.store._conn.execute('SELECT 1 FROM worker_quota_reservations WHERE account_id=? AND settled=0',
                                       (self.account_id,)).fetchone():
                raise ValueError('budget_requires_run_reconciliation')

    def apply(self, delta):
        self.store._conn.execute('UPDATE worker_quota_accounts SET calls=calls+?,tokens=tokens+?, '
                                'cost=cost+?,seconds=seconds+? WHERE account_id=?',
                                (delta['calls'], delta['tokens'], micros(round(delta['cost_yuan'], 6)),
                                 delta['active_seconds'], self.account_id))

    def progress(self, task):
        runs = [dict(r) for r in self.store._conn.execute(
            "SELECT r.run_id,r.terminal_state,substr(m.content,1,500) AS delivered_summary "
            "FROM runs r JOIN messages m ON m.run_id=r.run_id AND m.role='assistant' "
            "WHERE r.task_id=? ORDER BY r.created_at DESC LIMIT 10", (self.task_id,))]
        return (dict(accepted_steps=[s for s in task['steps'] if s['status'] == 'COMPLETED'],
                     delivered_runs=runs, note='已交付回答不等于步骤验收通过'),
                [s for s in task['steps'] if s['status'] != 'COMPLETED'])

    def blockers(self):
        row = TaskRuntime(self.tasks).snapshot(self.task_id)
        return ([row['recovery_error']] if row['recovery_error'] else []) + [
            '历史费用/活跃时间未计量时按初始全额；未决动作须先核实；加额后普通输入继续已选步骤。']

    def status(self):
        if not self.config.enabled and not exists(self.store, self.account_id):
            raise ValueError('budget_topup_disabled')
        return report(self)

    def approve(self, message_id, challenge):
        return authorize(self, message_id, challenge)


def effective_budget(store, task):
    key = 'single:' + task['task_id']
    if not exists(store, key):
        return task['budget']
    value = store._conn.execute('SELECT total FROM budget_topup_accounts WHERE account_id=?', (key,)).fetchone()
    total = spendable(json.loads(value[0]))
    return {**task['budget'], 'max_calls': total['calls'], 'max_estimated_tokens': total['tokens']}


def exhausted_reason(store, task):
    """Read-only /continue precheck; authoritative admission still runs under transaction."""
    key = 'single:' + task['task_id']
    if not exists(store, key):
        return None
    account = store._conn.execute('SELECT * FROM worker_quota_accounts WHERE account_id=?', (key,)).fetchone()
    runtime = store._conn.execute('SELECT used_calls,receipts FROM task_runtime WHERE task_id=?',
                                  (task['task_id'],)).fetchone()
    tokens = sum(r['estimated_tokens'] for r in json.loads(runtime['receipts'])) if runtime else 0
    if (account['cost'] <= account['spent_cost'] or account['seconds'] <= account['spent_seconds']
            or account['tokens'] <= tokens or account['calls'] <= (runtime['used_calls'] if runtime else 0)):
        return '预算已耗尽；/task budget 查看成果、未完成项与逐次授权续加入口'
    return None
