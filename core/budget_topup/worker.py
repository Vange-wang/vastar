"""Durable parent credits; detached child accounts never receive parent top-ups."""
import json

from core.budget_topup.service import authorize, report, repository
from core.orchestration.dispatch_gate import check


class WorkerTopup:
    def __init__(self, leases, config):
        self.leases, self.config = leases, config
        self.store = leases.store
        self.tasks, self.session_id = leases.plans.tasks, leases.plans.session_id
        self.task_id = leases.plans.task_id
        self.account_id = 'worker:' + self.task_id
        self.transaction = leases.transaction

    def repository(self):
        limit = self.leases.limit
        policy = json.loads(self.store._conn.execute('SELECT policy FROM worker_quota_policy WHERE task_id=?',
                                                     (self.task_id,)).fetchone()[0])
        return repository(self, dict(calls=limit.max_calls, tokens=limit.max_estimated_tokens,
                                     cost_yuan=policy[0] / 1000000, active_seconds=policy[1]), 'worker')

    def balance(self):
        calls, tokens = self.leases.repo.available()
        return dict(calls=calls, tokens=tokens, **self.leases.quotas.balance())

    def check(self):
        self.leases.plans._guard()
        check(self.leases)
        if self.store._conn.execute('SELECT 1 FROM worker_leases WHERE task_id=? AND settled=0',
                                     (self.task_id,)).fetchone():
            raise ValueError('budget_requires_workers_settled')

    def apply(self, delta):
        from memory.repositories.worker_quotas import micros
        self.store._conn.execute('UPDATE worker_quota_accounts SET calls=calls+?,tokens=tokens+?, '
                                'cost=cost+?,seconds=seconds+? WHERE account_id=?',
                                (delta['calls'], delta['tokens'], micros(round(delta['cost_yuan'], 6)),
                                 delta['active_seconds'], self.task_id))

    def progress(self, task):
        rows = self.leases.plans.snapshot()['records']
        completed = {k: v for k, v in rows.items() if v['state'] == 'accepted'}
        return completed, {k: v for k, v in rows.items() if k not in completed}

    def blockers(self):
        return ['未完成子任务仍须按原计划验收/重规划；加额不会绕过取消、依赖或返工上限。']

    def status(self):
        if not self.config.enabled:
            raise ValueError('budget_topup_disabled')
        return report(self)

    def approve(self, message_id, challenge):
        return authorize(self, message_id, challenge)


def annotate(result, runtime):
    from dataclasses import replace
    detail = str(runtime.errors) + str(getattr(runtime, 'warnings', []))
    if 'budget' in detail.lower() or '预算' in detail:
        return replace(result, blockers=result.blockers + (
            '预算已耗尽或不足；/task budget worker 查看已完成成果和未完成项，逐次授权续加。',))
    return result
