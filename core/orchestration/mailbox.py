"""Durable directed outbox/inbox, separate delivery ACK and task-input consumption.

All entry points are trusted-host APIs; no tool exposes identity or profile edits.
Transport retries reuse the immutable message ID. Offline targets accept delivery.
"""
import time
from dataclasses import asdict

from core.orchestration.contracts import positive
from core.orchestration.team_contracts import TeamMessage


class Mailbox:
    def __init__(self, registry, *, clock=time.time, capacity=128, max_attempts=3):
        positive(capacity, 'mailbox_capacity')
        positive(max_attempts, 'mailbox_attempts')
        self.registry, self.repo, self.store = registry, registry.repo, registry.store
        self.clock, self.capacity, self.max_attempts = clock, capacity, max_attempts

    def send(self, message, *, sender):
        if type(message) is not TeamMessage or sender != message.sender:
            raise ValueError('team_message_sender_mismatch')
        with self.store.transaction():
            source = self.registry.member(sender, active=True)
            target = self.registry.member(message.recipient)
            if source['team_id'] != target['team_id'] or message.task != target['root_task']:
                raise ValueError('team_message_scope_mismatch')
            prior = next((r for r in self.repo.all('team_messages')
                          if r['message']['message_id'] == message.message_id), None)
            if prior:
                if prior['message'] != asdict(message):
                    raise ValueError('team_message_id_conflict')
                return prior
            if message.created > self.clock():
                raise ValueError('team_message_future_created')
            assignment = self.registry.assignment(message.task, message.step_id)
            if assignment['member_id'] != message.recipient:
                raise ValueError('team_message_not_task_owner')
            self.sweep()
            pending = [r for r in self.repo.all('team_messages')
                       if r['message']['recipient'] == message.recipient
                       and r['state'] in ('outbox', 'delivered')]
            admitted = [r for r in self.repo.all('team_inputs')
                        if r['recipient'] == message.recipient and r['status'] == 'pending']
            if len(pending) + len(admitted) >= self.capacity:
                raise ValueError('team_mailbox_backpressure')
            value = dict(message=asdict(message), team_id=target['team_id'],
                         generation=target['generation'], assignment_generation=assignment['generation'],
                         profile_version=assignment['profile_version'],
                         state='outbox', ack=False, acknowledged_at=None, consumed_at=None,
                         attempts=0, reason=None)
            self.repo.put('team_messages', message.message_id, value, create=True)
            return value

    def get(self, message_id):
        value = self.repo.get('team_messages', message_id)
        self.registry.team(value['team_id'])
        return value

    def deliver(self, message_id):
        with self.store.transaction():
            value = self.get(message_id)
            if value['state'] != 'outbox':
                return value
            if value['message']['expiry'] <= self.clock():
                return self._dead(value, 'expired')
            value.update(state='delivered', ack=True, acknowledged_at=self.clock())
            self.repo.put('team_messages', message_id, value)
            return value

    def retry_outbox(self):
        return [self.deliver(r['message']['message_id']) for r in self.repo.all('team_messages')
                if r['state'] == 'outbox' and r['team_id'] in self._teams()]

    def _teams(self):
        return {r['team_id'] for r in self.repo.all('teams') if r['owner'] == self.registry.owner}

    def inbox(self, recipient):
        self.registry.member(recipient)
        self.sweep()
        return [r for r in self.repo.all('team_messages')
                if r['message']['recipient'] == recipient and r['state'] == 'delivered']

    def sweep(self):
        with self.store.transaction():
            for value in self.repo.all('team_messages'):
                if (value['team_id'] in self._teams() and value['state'] in ('outbox', 'delivered')
                        and value['message']['expiry'] <= self.clock()):
                    self._dead(value, 'expired')

    def _dead(self, value, reason):
        value.update(state='dead', reason=reason)
        self.repo.put('team_messages', value['message']['message_id'], value)
        return value

    def failure(self, message_id, *, recipient, reason):
        """Bounded poison handling, durable attempts and dead letter; no silent drop."""
        with self.store.transaction():
            value = self.get(message_id)
            if value['message']['recipient'] != recipient:
                raise ValueError('team_message_recipient_mismatch')
            if value['state'] != 'delivered':
                raise ValueError('team_message_not_delivered')
            value['attempts'] += 1
            value['reason'] = str(reason)[:256]
            if value['attempts'] >= self.max_attempts:
                return self._dead(value, 'poison')
            self.repo.put('team_messages', message_id, value)
            return value
