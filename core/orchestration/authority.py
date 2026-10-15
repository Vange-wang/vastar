"""Frozen original permissions outlive parent ownership after an explicit transfer."""
from core.orchestration.permissions import intersect


def guard(scheduler, lease, envelope, profile, effective, authority, reservation=None):
    def revalidate():
        if scheduler.identity_check is not None:
            scheduler.identity_check(envelope, profile)
        detached = reservation in scheduler.budget_pool._detached
        if lease is not None:
            lease.check()
            detached = lease.service.quotas.row(lease.token.lease_id)['detached']
        if detached:
            scheduler._validate_memory(envelope, authority, effective)
            if (not scheduler.config.enabled or intersect(envelope, profile, authority.permissions,
                    scheduler.sandbox, scheduler.tools.registry) != effective):
                raise ValueError('worker_authority_changed')
            return
        if scheduler.parent != authority or scheduler._validate(envelope, profile) != effective:
            raise ValueError('worker_authority_changed')
    return revalidate
