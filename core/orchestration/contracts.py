"""C08 v1 immutable boundary values; no parent chat, credentials or live handles."""
from dataclasses import dataclass, field
from typing import Protocol


def nonempty(value, name):
    if type(value) is not str or not value.strip():
        raise ValueError(f'worker_{name}_invalid')


def positive(value, name):
    if type(value) is not int or value < 1:
        raise ValueError(f'worker_{name}_invalid')


def strings(value, name):
    if type(value) is not tuple:
        raise ValueError(f'worker_{name}_must_be_unique_tuple')
    for item in value:
        nonempty(item, name)
    if len(set(value)) != len(value):
        raise ValueError(f'worker_{name}_must_be_unique_tuple')


@dataclass(frozen=True)
class Budget:
    max_calls: int
    max_estimated_tokens: int
    deadline_seconds: int = 60
    max_cost_yuan: float | None = None
    max_active_seconds: float | None = None

    def __post_init__(self):
        import math
        for key in ('max_cost_yuan', 'max_active_seconds'):
            value = getattr(self, key)
            if value is not None and (type(value) not in (int, float)
                                     or not math.isfinite(value) or value <= 0):
                raise ValueError(f'worker_{key}_invalid')
        for key in ('max_calls', 'max_estimated_tokens', 'deadline_seconds'):
            positive(getattr(self, key), key)

    def fits(self, ceiling: 'Budget') -> bool:
        return (all(getattr(self, key) <= getattr(ceiling, key)
                    for key in ('max_calls', 'max_estimated_tokens', 'deadline_seconds'))
                and all(getattr(ceiling, key) is None or (getattr(self, key) is not None
                        and getattr(self, key) <= getattr(ceiling, key))
                        for key in ('max_cost_yuan', 'max_active_seconds')))


@dataclass(frozen=True)
class Permissions:
    tools: tuple[str, ...] = ()
    read_scope: tuple[str, ...] = ()
    write_scope: tuple[str, ...] = ()
    memory_scope: tuple[str, ...] = ()

    def __post_init__(self):
        for key in ('tools', 'read_scope', 'write_scope', 'memory_scope'):
            strings(getattr(self, key), key)


@dataclass(frozen=True)
class WorkerProfile:
    profile_id: str
    role: str
    model_capabilities: tuple[str, ...]
    permissions: Permissions
    budget: Budget
    delegation_depth: int = 1

    def __post_init__(self):
        nonempty(self.profile_id, 'profile_id')
        if self.role not in ('worker', 'reviewer'):
            raise ValueError('worker_role_unsupported')
        strings(self.model_capabilities, 'model_capabilities')
        if 'stream' not in self.model_capabilities:
            raise ValueError('worker_stream_capability_required')
        positive(self.delegation_depth, 'delegation_depth')
        if type(self.permissions) is not Permissions or type(self.budget) is not Budget:
            raise ValueError('worker_profile_contract_invalid')
        if self.role == 'reviewer' and self.permissions.write_scope:
            raise ValueError('reviewer_must_be_readonly')


@dataclass(frozen=True)
class TaskEnvelope:
    task_id: str
    step_id: str
    parent_step_id: str
    goal: str
    base_revision: str
    plan_revision: int
    owner_generation: int
    budget: Budget
    acceptance: tuple[str, ...]
    input_refs: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    read_scope: tuple[str, ...] = ()
    write_scope: tuple[str, ...] = ()
    memory_snapshot: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    delegation_depth: int = 1
    context_mode: str = 'fresh'
    source_snapshot: str | None = None
    deadline_policy: str = 'fail'
    schema_version: int = 1

    def __post_init__(self):
        for key in ('task_id', 'step_id', 'parent_step_id', 'goal', 'base_revision'):
            nonempty(getattr(self, key), key)
        for key in ('plan_revision', 'owner_generation', 'delegation_depth'):
            positive(getattr(self, key), key)
        for key in ('acceptance', 'input_refs', 'depends_on', 'allowed_tools',
                    'read_scope', 'write_scope', 'memory_snapshot', 'constraints'):
            strings(getattr(self, key), key)
        if not self.acceptance or type(self.budget) is not Budget:
            raise ValueError('worker_acceptance_or_budget_missing')
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError('worker_envelope_version_unsupported')
        if self.context_mode not in ('fresh', 'fork') or self.deadline_policy != 'fail':
            raise ValueError('worker_context_or_deadline_policy_invalid')
        if (self.context_mode == 'fork') != (self.source_snapshot is not None):
            raise ValueError('worker_fork_requires_explicit_snapshot')
        if self.source_snapshot is not None:
            nonempty(self.source_snapshot, 'source_snapshot')


@dataclass(frozen=True)
class PlanRevision:
    """A Lead proposal is inert data, never a dispatched job."""
    task_id: str
    revision: int
    steps: tuple[TaskEnvelope, ...]

    def __post_init__(self):
        nonempty(self.task_id, 'task_id')
        positive(self.revision, 'plan_revision')
        if type(self.steps) is not tuple or not self.steps:
            raise ValueError('worker_plan_steps_invalid')
        if any(type(step) is not TaskEnvelope for step in self.steps):
            raise ValueError('worker_plan_envelope_invalid')
        if len({step.step_id for step in self.steps}) != len(self.steps):
            raise ValueError('worker_duplicate_step')


class Lead(Protocol):
    """Planner boundary: proposals only. No Scheduler or tool handle is an input."""

    async def propose(self, goal: str) -> PlanRevision: ...


@dataclass(frozen=True)
class TaskResult:
    status: str
    summary: str
    artifact_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    remaining_work: tuple[str, ...]
    blockers: tuple[str, ...]
    changed_paths: tuple[str, ...]
    base_revision: str
    plan_revision: int
    owner_generation: int
    actual_usage: dict | None = field(default=None, compare=False)

    def __post_init__(self):
        if self.status not in ('completed', 'incomplete', 'failed', 'cancelled', 'blocked'):
            raise ValueError('worker_result_status_invalid')
        if type(self.summary) is not str:
            raise ValueError('worker_result_summary_invalid')
        for key in ('artifact_refs', 'evidence_refs', 'remaining_work', 'blockers', 'changed_paths'):
            strings(getattr(self, key), key)
        nonempty(self.base_revision, 'base_revision')
        positive(self.plan_revision, 'plan_revision')
        positive(self.owner_generation, 'owner_generation')
