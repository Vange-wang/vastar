"""Exactly-once durable input admission, not a promise of exactly-once external effects.

The immutable assignment, current DAG, current host authority and selected evidence
must all agree. An input carries a reference only: no tools/permissions/profile edits.
Actual execution still uses Scheduler/leases and B47/B48 publication gates.
"""
from dataclasses import asdict

from core.orchestration.team_contracts import profile_from
from memory.repositories.tasks import encode


class TeamInputs:
    def __init__(self, mailbox, *, shared_memory=None):
        self.mailbox, self.registry = mailbox, mailbox.registry
        self.repo, self.store = mailbox.repo, mailbox.store
        self.shared_memory = shared_memory

    def consume(self, message_id, *, recipient, scheduler):
        with self.store.transaction():
            value = self.mailbox.get(message_id)
            msg = value['message']
            if recipient != msg['recipient']:
                raise ValueError('team_message_recipient_mismatch')
            if value['state'] == 'consumed':
                return self.repo.get('team_inputs', message_id)
            if value['state'] != 'delivered' or msg['expiry'] <= self.mailbox.clock():
                raise ValueError('team_message_not_live_delivered')
            envelope, profile, evidence = self._validate(value, scheduler)
            self._causation(msg)
            result = dict(message_id=message_id, recipient=recipient, task=msg['task'],
                          step_id=msg['step_id'], plan_revision=msg['plan_revision'],
                          payload_ref=msg['payload_ref'], payload_digest=evidence.digest,
                          profile_version=value['profile_version'], generation=value['generation'],
                          assignment_generation=value['assignment_generation'],
                          status='pending', origin='team_input', authority='none')
            self.repo.put('team_inputs', message_id, result, create=True)
            value.update(state='consumed', consumed_at=self.mailbox.clock())
            self.repo.put('team_messages', message_id, value)
            return result

    def _validate(self, value, scheduler):
        msg = value['message']
        member = self.registry.member(msg['recipient'], active=True)
        assignment = self.registry.assignment(msg['task'], msg['step_id'])
        if (assignment['member_id'] != member['member_id'] or assignment['status'] != 'assigned'
                or assignment['generation'] != value.get('assignment_generation')
                or member['generation'] != value['generation']
                or member['profile_version'] != value['profile_version']
                or assignment['profile_version'] != value['profile_version']):
            raise ValueError('team_identity_or_profile_stale')
        envelope = self.registry.envelope(msg['task'], msg['step_id'])
        if msg['plan_revision'] != envelope.plan_revision:
            raise ValueError('team_message_revision_stale')
        if encode(asdict(envelope)) != encode(assignment['envelope']):
            raise ValueError('team_assignment_contract_changed')
        profile = profile_from(member['profile'])
        effective = scheduler._validate(envelope, profile)
        selected = scheduler.store.select(envelope, scheduler.parent.evidence_refs, effective.memory_scope)
        refs = tuple(item.ref for item in selected if item.kind == 'memory')
        if refs:
            if (self.shared_memory is None or self.shared_memory.store is not self.store
                    or self.shared_memory.project_id != self.store.task_get(msg['task'])['project_id']):
                raise ValueError('team_shared_memory_revalidator_required')
            report = self.shared_memory.revalidate(refs)
            if report['stale'] or report['revoked'] or report['unknown']:
                raise ValueError('team_shared_memory_not_current')
        # Sender cannot introduce references absent from the host-approved envelope.
        if msg['payload_ref'] not in envelope.input_refs + envelope.memory_snapshot:
            raise ValueError('team_payload_not_task_input')
        evidence = scheduler.store.get(msg['payload_ref'], scheduler.parent.evidence_refs,
                                       effective.memory_scope)
        return envelope, profile, evidence

    def _causation(self, msg):
        if msg['causation_id'] == 'task:' + msg['task']:
            return
        cause = self.mailbox.get(msg['causation_id'])
        current = self.mailbox.get(msg['message_id'])
        if (cause['team_id'] != current['team_id'] or cause['state'] != 'consumed'
                or cause['message']['task'] != msg['task']
                or cause['message']['plan_revision'] != msg['plan_revision']):
            raise ValueError('team_causation_not_consumed_or_stale')

    def pending(self, recipient):
        self.registry.member(recipient)
        return [r for r in self.repo.all('team_inputs')
                if r['recipient'] == recipient and r['status'] == 'pending']

    def prepare(self, message_id, *, recipient, scheduler):
        """Recovery read: revalidate without re-consuming; host may feed Scheduler.

Inputs already reference the frozen envelope, so preparing cannot broaden context.
Pending inputs remain until the host observes the persisted DAG result.
"""
        with self.store.transaction():
            record = self.repo.get('team_inputs', message_id)
            if record['recipient'] != recipient or record['status'] != 'pending':
                raise ValueError('team_input_not_pending_for_member')
            envelope, profile, evidence = self._validate(self.mailbox.get(message_id), scheduler)
            if evidence.digest != record['payload_digest']:
                raise ValueError('team_payload_changed')
            return envelope, profile

    def settle(self, message_id, *, recipient):
        with self.store.transaction():
            record = self.repo.get('team_inputs', message_id)
            if record['recipient'] != recipient:
                raise ValueError('team_input_recipient_mismatch')
            self.registry.member(recipient)
            assignment = self.registry.assignment(record['task'], record['step_id'])
            if (assignment['member_id'] != recipient
                    or assignment['generation'] != record.get('assignment_generation')
                    or assignment['profile_version'] != record['profile_version']):
                raise ValueError('team_input_ownership_changed')
            plan = self.registry.plans.read(record['task'])
            state = plan['records'][record['step_id']]['state']
            if plan['plan']['revision'] != record['plan_revision'] or state not in (
                    'reported', 'accepted', 'completed', 'failed', 'cancelled', 'blocked', 'incomplete'):
                raise ValueError('team_input_result_not_persisted')
            record.update(status='settled', result_state=state)
            self.repo.put('team_inputs', message_id, record)
            return record

    async def dispatch(self, message_id, *, recipient, scheduler, accept=None, emit=None):
        """Host adapter: reuse Scheduler leases, runtime, quotas and publication gates.

        A restart reads the durable DAG result; it never silently reruns a reported step.
        An interrupted running lease still requires the existing B45 recovery protocol.
        """
        from copy import copy
        from uuid import uuid4

        envelope, profile = self.prepare(message_id, recipient=recipient, scheduler=scheduler)
        plan = scheduler.plans.current()
        if envelope not in plan.steps:
            raise ValueError('team_scheduler_plan_mismatch')
        runtime_id = uuid4().hex
        self.registry.connect(recipient, runtime_id)
        scoped = copy(scheduler)

        def check(current, current_profile):
            if scheduler.identity_check is not None:
                scheduler.identity_check(current, current_profile)
            expected, expected_profile, _ = self._validate(self.mailbox.get(message_id), scheduler)
            if (current != expected or current_profile != expected_profile
                    or self.registry.member(recipient)['runtime_id'] != runtime_id):
                raise ValueError('team_runtime_identity_changed')

        scoped.identity_check = check
        try:
            result = await scoped.dispatch(plan, profile, envelope.step_id, accept=accept, emit=emit)
            self.settle(message_id, recipient=recipient)
            return result
        finally:
            member = self.registry.member(recipient)
            if member['runtime_id'] == runtime_id:
                self.registry.disconnect(recipient, runtime_id)
