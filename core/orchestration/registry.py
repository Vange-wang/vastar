"""Host-managed persistent members, separate from ephemeral ChildSupervisor runs."""
from dataclasses import asdict

from core.orchestration.contracts import WorkerProfile, nonempty, positive
from core.orchestration.plans import decode_plan
from core.orchestration.team_contracts import profile_from
from memory.repositories.team_state import TeamState
from memory.repositories.worker_plans import WorkerPlanRepository


class AgentRegistry:
    def __init__(self, store, *, owner):
        nonempty(owner, 'team_owner')
        self.store, self.owner = store, owner
        self.repo = TeamState(store)
        self.plans = WorkerPlanRepository(store)

    def create_team(self, team_id, root_task):
        nonempty(team_id, 'team_id')
        with self.store.transaction():
            task = self.store.task_get(root_task)
            if task['status'] in ('COMPLETED', 'CANCELLED'):
                raise ValueError('team_root_closed')
            self.repo.put('teams', team_id, dict(team_id=team_id, root_task=root_task,
                          owner=self.owner), create=True)

    def team(self, team_id):
        value = self.repo.get('teams', team_id)
        if value['owner'] != self.owner:
            raise ValueError('team_owner_mismatch')
        return value

    def join(self, team_id, member_id, profile, *, profile_version=1):
        nonempty(member_id, 'member_id')
        positive(profile_version, 'profile_version')
        if type(profile) is not WorkerProfile:
            raise ValueError('team_profile_invalid')
        with self.store.transaction():
            team = self.team(team_id)
            self.repo.put('team_members', member_id, dict(**team, member_id=member_id,
                          kind='persistent', profile_version=profile_version,
                          profile=asdict(profile), status='active', generation=1,
                          runtime_id=None, runtime_epoch=None, history=[]), create=True)

    def member(self, member_id, *, active=False):
        value = self.repo.get('team_members', member_id)
        self.team(value['team_id'])
        if active and value['status'] != 'active':
            raise ValueError('team_member_inactive')
        return value

    def connect(self, member_id, runtime_id):
        nonempty(runtime_id, 'runtime_id')
        with self.store.transaction():
            value = self.member(member_id, active=True)
            if self.online(member_id) and value['runtime_id'] != runtime_id:
                raise ValueError('team_member_already_online')
            value.update(runtime_id=runtime_id, runtime_epoch=self.store._owner.owner_id)
            self.repo.put('team_members', member_id, value)

    def disconnect(self, member_id, runtime_id):
        with self.store.transaction():
            value = self.member(member_id)
            if value['runtime_id'] != runtime_id:
                raise ValueError('team_runtime_stale')
            value.update(runtime_id=None, runtime_epoch=None)
            self.repo.put('team_members', member_id, value)

    def online(self, member_id):
        value = self.member(member_id)
        return (value['status'] == 'active' and value['runtime_id'] is not None
                and value['runtime_epoch'] == self.store._owner.owner_id)

    def envelope(self, task_id, step_id):
        task = self.store.task_get(task_id)
        if task['status'] in ('COMPLETED', 'CANCELLED', 'BLOCKED'):
            raise ValueError('team_task_unavailable')
        plan = decode_plan(self.plans.read(task_id)['plan'])
        step = next((s for s in plan.steps if s.step_id == step_id), None)
        if step is None:
            raise ValueError('team_step_missing')
        return step

    @staticmethod
    def key(task_id, step_id):
        import json
        return json.dumps([task_id, step_id])

    def assign(self, member_id, task_id, step_id, scheduler):
        with self.store.transaction():
            value = self.member(member_id, active=True)
            if value['root_task'] != task_id:
                raise ValueError('team_task_root_mismatch')
            envelope = self.envelope(task_id, step_id)
            scheduler._validate(envelope, profile_from(value['profile']))
            key = self.key(task_id, step_id)
            self.repo.put('team_assignments', key, dict(task=task_id, step_id=step_id,
                          member_id=member_id, owner=value['owner'], team_id=value['team_id'],
                          generation=value['generation'], profile_version=value['profile_version'],
                          envelope=asdict(envelope), status='assigned'), create=True)

    def assignment(self, task_id, step_id):
        return self.repo.get('team_assignments', self.key(task_id, step_id))

    def disable(self, member_id):
        """Keep every assignment and queued message; explicit retained owner, no orphan deletion."""
        with self.store.transaction():
            value = self.member(member_id)
            value.update(status='disabled', runtime_id=None, runtime_epoch=None)
            self.repo.put('team_members', member_id, value)
            for assignment in self.pending(member_id):
                assignment['status'] = 'retained_disabled'
                self.repo.put('team_assignments', self.key(assignment['task'], assignment['step_id']), assignment)

    def pending(self, member_id):
        self.member(member_id)
        return [a for a in self.repo.all('team_assignments') if a['member_id'] == member_id]

    def grow(self, member_id, profile, *, expected_version):
        """Explicit host revision; existing tasks remain pinned until explicit reassignment."""
        if type(profile) is not WorkerProfile:
            raise ValueError('team_profile_invalid')
        with self.store.transaction():
            value = self.member(member_id, active=True)
            if value['profile_version'] != expected_version:
                raise ValueError('team_profile_version_conflict')
            value['history'].append(dict(version=expected_version, profile=value['profile']))
            value.update(profile=asdict(profile), profile_version=expected_version + 1)
            self.repo.put('team_members', member_id, value)

    def transfer(self, task_id, step_id, target, scheduler):
        """Host-only, quiescent reassignment; no message is silently retargeted."""
        with self.store.transaction():
            old = self.assignment(task_id, step_id)
            member = self.member(target, active=True)
            if old['team_id'] != member['team_id']:
                raise ValueError('team_transfer_outside_team')
            record = self.plans.read(task_id)['records'][step_id]
            if record['state'] == 'running':
                raise ValueError('team_transfer_requires_quiescence')
            envelope = self.envelope(task_id, step_id)
            scheduler._validate(envelope, profile_from(member['profile']))
            old.setdefault('history', []).append(dict(member_id=old['member_id'],
                                                      profile_version=old['profile_version']))
            old.update(member_id=target, generation=old['generation'] + 1,
                       profile_version=member['profile_version'], envelope=asdict(envelope), status='assigned')
            self.repo.put('team_assignments', self.key(task_id, step_id), old)

