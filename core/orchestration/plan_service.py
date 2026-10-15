"""TaskService's opt-in child DAG: proposals, CAS, claims and explicit host acceptance."""
import time
from copy import deepcopy
from dataclasses import asdict

from core.orchestration.config import OrchestrationConfig
from core.orchestration.contracts import TaskResult
from core.orchestration.plans import (
    MAX_REWORK,
    decode_plan,
    encode_plan,
    ready_order,
    validate_plan,
    validate_replan,
)
from memory.repositories.worker_plans import WorkerPlanRepository


class PlanService:
    def __init__(self, task_service, session_id, config, *, clock=time.time):
        if type(config) is not OrchestrationConfig:
            raise ValueError('worker_scheduler_config_invalid')
        if not config.enabled:
            raise ValueError('worker_orchestration_disabled')
        self.tasks, self.session_id, self.config = task_service, session_id, config
        self.task_id = task_service.current(session_id)['task_id']
        self.clock = clock
        self.repo = WorkerPlanRepository(task_service.store)

    def _guard(self):
        if not self.config.enabled:
            raise ValueError('worker_orchestration_disabled')
        task = self.tasks.current(self.session_id)
        if task['task_id'] != self.task_id or task['status'] in {'COMPLETED', 'CANCELLED', 'BLOCKED'}:
            raise ValueError('dag_parent_task_unavailable')

    def snapshot(self):
        self._guard()
        return self.repo.read(self.task_id)

    def current(self):
        return decode_plan(self.snapshot()['plan'])

    def _load(self, expected):
        value = self.snapshot()
        if type(expected) is not int or value['plan']['revision'] != expected:
            raise ValueError('dag_cas_conflict')
        return value

    def _priorities(self, plan, priorities):
        priorities = priorities or {}
        if set(priorities) - {s.step_id for s in plan.steps}:
            raise ValueError('dag_priority_unknown_step')
        if any(type(v) is not int or not 0 <= v <= 10 for v in priorities.values()):
            raise ValueError('dag_priority_out_of_range')
        return priorities

    def create(self, plan, *, priorities=None):
        validate_plan(plan)
        if plan.task_id != self.task_id or plan.revision != 1:
            raise ValueError('dag_initial_plan_mismatch')
        priorities = self._priorities(plan, priorities)
        with self.tasks.store.transaction():
            self._guard()
            records = {s.step_id: self._queued(priorities.get(s.step_id, 0)) for s in plan.steps}
            self.repo.write(self.task_id, dict(schema_version=1, plan=encode_plan(plan),
                            records=records, replans=0, cause=None), 0)
        return plan

    def _queued(self, priority, attempts=0):
        return dict(state='queued', result=None, priority=priority, attempts=attempts,
                    queued_at=self.clock(), accepted_revision=None)

    def replan(self, plan, *, expected_revision, affected, invalidated=(), trigger,
               evidence, verify, priorities=None):
        """Lead data is inert; a trusted host verifier must substantiate the trigger."""
        priorities = self._priorities(plan, priorities)
        with self.tasks.store.transaction():
            value = self._load(expected_revision)
            old = decode_plan(value['plan'])
            records = value['records']
            if any(r['state'] == 'running' for r in records.values()):
                raise ValueError('dag_replan_requires_quiescence')
            if value['replans'] >= MAX_REWORK:
                raise ValueError('dag_rework_limit')
            validate_replan(old, plan, records, affected=affected, invalidated=invalidated,
                            trigger=trigger, evidence=evidence)
            if not callable(verify) or verify(trigger, evidence) is not True:
                raise ValueError('dag_trigger_not_verified')
            if self.snapshot() != value:
                raise ValueError('dag_verifier_changed_state')
            # Priority changes also belong to the declared affected subgraph.
            if any(k not in affected and v != records[k]['priority'] for k, v in priorities.items()):
                raise ValueError('dag_priority_outside_subgraph')
            self.repo.archive(self.task_id, value)
            new_records = {}
            for step in plan.steps:
                key = step.step_id
                if key in records and key not in affected:
                    new_records[key] = records[key]
                else:
                    prior = records.get(key, {})
                    new_records[key] = self._queued(priorities.get(key, prior.get('priority', 0)),
                                                    prior.get('attempts', 0))
            value.update(plan=encode_plan(plan), records=new_records, replans=value['replans'] + 1,
                         cause=dict(trigger=trigger, evidence=evidence, affected=affected,
                                    invalidated=invalidated))
            self.repo.write(self.task_id, value, expected_revision)
        return plan

    def ready(self):
        value = self.snapshot()
        return ready_order(decode_plan(value['plan']), value['records'], self.clock())

    def claim(self, plan, step_id):
        with self.tasks.store.transaction():
            from memory.repositories.worker_leases import has_account
            if has_account(self.tasks.store, self.task_id):
                raise ValueError('worker_lease_required')
            value = self._load(plan.revision)
            if decode_plan(value['plan']) != plan:
                raise ValueError('dag_plan_content_mismatch')
            if step_id not in ready_order(plan, value['records'], self.clock()):
                raise ValueError('dag_step_not_ready_or_accepted')
            if sum(r['state'] == 'running' for r in value['records'].values()) >= self.config.max_workers:
                raise ValueError('dag_capacity_exceeded')
            row = value['records'][step_id]
            row.update(state='running', attempts=row['attempts'] + 1)
            self.repo.write(self.task_id, value, plan.revision)

    def record(self, envelope, result, *, lease=None):
        with self.tasks.store.transaction():
            value = self._load(envelope.plan_revision)
            row = value['records'][envelope.step_id]
            if row.get('lease_id'):
                if lease is None or lease.token.lease_id != row['lease_id']:
                    raise ValueError('worker_lease_required')
                lease.check(allow_settled=True)
            expected = next(s for s in decode_plan(value['plan']).steps if s.step_id == envelope.step_id)
            if expected != envelope or row['state'] != 'running' or type(result) is not TaskResult:
                raise ValueError('dag_result_mismatch_or_duplicate')
            if any(getattr(result, key) != getattr(envelope, key)
                   for key in ('base_revision', 'plan_revision', 'owner_generation')):
                raise ValueError('dag_result_version_mismatch')
            row.update(state='reported' if result.status == 'completed' else result.status,
                       result=asdict(result))
            self.repo.write(self.task_id, value, envelope.plan_revision)

    def accept_step(self, step_id, *, expected_revision, verify):
        with self.tasks.store.transaction():
            value = self._load(expected_revision)
            row = value['records'].get(step_id)
            if not row or row['state'] != 'reported':
                raise ValueError('dag_result_not_reported')
            result = row['result']
            from core.orchestration.integrator import require_integrated_result
            envelope = next(s for s in decode_plan(value['plan']).steps if s.step_id == step_id)
            require_integrated_result(envelope, result)
            if (not result['evidence_refs'] or result['remaining_work'] or result['blockers']
                    or not callable(verify) or verify(deepcopy(result)) is not True):
                raise ValueError('dag_acceptance_failed')
            if self.snapshot() != value:
                raise ValueError('dag_verifier_changed_state')
            row.update(state='accepted', accepted_revision=expected_revision)
            self.repo.write(self.task_id, value, expected_revision)

    def assert_running(self, envelope):
        value = self._load(envelope.plan_revision)
        if value['records'][envelope.step_id].get('lease_id'):
            raise ValueError('worker_lease_required')
        if value['records'][envelope.step_id]['state'] != 'running':
            raise ValueError('dag_claim_lost')
