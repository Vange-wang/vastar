"""Persistent identity and inert directed message contracts; never authorization."""
import math
from dataclasses import dataclass

from core.orchestration.contracts import Budget, Permissions, WorkerProfile, nonempty, positive


def profile_from(value):
    value = dict(value)
    value['permissions'] = Permissions(**{k: tuple(v) for k, v in value['permissions'].items()})
    value['budget'] = Budget(**value['budget'])
    value['model_capabilities'] = tuple(value['model_capabilities'])
    return WorkerProfile(**value)


@dataclass(frozen=True)
class TeamMessage:
    message_id: str
    sender: str
    recipient: str
    causation_id: str
    task: str
    step_id: str
    plan_revision: int
    payload_ref: str
    created: float
    expiry: float

    def __post_init__(self):
        for key in ('message_id', 'sender', 'recipient', 'causation_id', 'task', 'step_id', 'payload_ref'):
            nonempty(getattr(self, key), key)
            if len(getattr(self, key)) > 512 or getattr(self, key) == '*':
                raise ValueError('team_message_target_or_size_invalid')
        positive(self.plan_revision, 'plan_revision')
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in (self.created, self.expiry)):
            raise ValueError('team_message_time_invalid')
        if self.expiry <= self.created:
            raise ValueError('team_message_expiry_invalid')
