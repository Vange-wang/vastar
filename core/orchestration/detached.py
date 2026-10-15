"""Durable independent owner/account while retaining original envelope provenance."""
import json
from dataclasses import asdict

from core.orchestration.contracts import TaskResult, nonempty


def transfer(lease, owner):
    nonempty(owner, 'detach_owner')
    with lease.service.transaction():
        from core.orchestration.dispatch_gate import check
        check(lease.service)
        row = lease.check()
        return lease.service.quotas.transfer(row, owner)


def record(lease, envelope, result):
    service = lease.service
    row = lease.check(allow_settled=True)
    if not row['settled'] or type(result) is not TaskResult:
        raise ValueError('worker_settlement_required')
    value = service.plans.repo.read(lease.token.task_id)
    if any(getattr(result, k) != getattr(envelope, k) for k in
           ('base_revision', 'plan_revision', 'owner_generation')):
        raise ValueError('dag_result_version_mismatch')
    encoded = json.dumps(asdict(result), ensure_ascii=False)
    service.store._conn.execute('UPDATE worker_quota_reservations SET result=? WHERE lease_id=?',
                                (encoded, lease.token.lease_id))
    entry = value['records'][lease.token.step_id]
    entry.update(state='reported' if result.status == 'completed' else result.status, result=asdict(result))
    service.plans.repo.write(lease.token.task_id, value, value['plan']['revision'])
    service.repo.close(lease.token.lease_id, 'finished')
