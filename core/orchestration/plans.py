"""B44 deterministic DAG proposals. This module has no execution authority."""
from dataclasses import asdict, replace

from core.orchestration.contracts import Budget, PlanRevision, TaskEnvelope, strings

TRIGGERS = frozenset({'premise_disproved', 'artifact_invalid', 'no_progress', 'budget_insufficient'})
MAX_STEPS = 256
MAX_REWORK = 2


def validate_plan(plan):
    if type(plan) is not PlanRevision or len(plan.steps) > MAX_STEPS:
        raise ValueError('dag_plan_invalid')
    steps = {s.step_id: s for s in plan.steps}
    for step in plan.steps:
        if step.task_id != plan.task_id or step.plan_revision != plan.revision:
            raise ValueError('dag_step_revision_mismatch')
        if set(step.depends_on) - steps.keys():
            raise ValueError('dag_dangling_dependency')
    pending, ordered = dict(steps), []
    while pending:
        ready = [key for key, step in pending.items() if set(step.depends_on) <= set(ordered)]
        if not ready:
            raise ValueError('dag_cycle')
        ordered.extend(ready)
        for key in ready:
            del pending[key]
    return ordered


def encode_plan(plan):
    return asdict(plan)


def decode_plan(value):
    steps = []
    for item in value['steps']:
        fields = dict(item)
        fields['budget'] = Budget(**fields['budget'])
        for key in ('acceptance', 'input_refs', 'depends_on', 'allowed_tools', 'read_scope',
                    'write_scope', 'memory_snapshot', 'constraints'):
            fields[key] = tuple(fields[key])
        steps.append(TaskEnvelope(**fields))
    plan = PlanRevision(value['task_id'], value['revision'], tuple(steps))
    validate_plan(plan)
    return plan


def descendants(plan, roots):
    found = set(roots)
    for key in validate_plan(plan):
        step = next(s for s in plan.steps if s.step_id == key)
        if set(step.depends_on) & found:
            found.add(key)
    return found


def validate_replan(old, new, records, *, affected, invalidated, trigger, evidence):
    validate_plan(new)
    strings(affected, 'affected')
    strings(invalidated, 'invalidated')
    strings(evidence, 'replan_evidence')
    if not affected or not evidence or trigger not in TRIGGERS:
        raise ValueError('dag_replan_trigger_or_evidence_required')
    if new.task_id != old.task_id or new.revision != old.revision + 1:
        raise ValueError('dag_revision_mismatch')
    before, after = ({s.step_id: s for s in p.steps} for p in (old, new))
    changed = {k for k in before.keys() | after.keys()
               if k not in before or k not in after
               or replace(before[k], plan_revision=new.revision) != after[k]}
    if set(affected) - (before.keys() | after.keys()) or not changed <= set(affected):
        raise ValueError('dag_affected_subgraph_incomplete')
    required = descendants(old, set(affected) & before.keys())
    required |= descendants(new, set(affected) & after.keys())
    if not required <= set(affected):
        raise ValueError('dag_affected_descendants_missing')
    accepted = {k for k, r in records.items() if r['state'] == 'accepted'}
    if not set(invalidated) <= (accepted & set(affected)):
        raise ValueError('dag_invalidated_not_accepted')
    if accepted & set(affected) != set(invalidated):
        raise ValueError('dag_completed_premise_requires_invalidation')
    if invalidated and trigger not in {'premise_disproved', 'artifact_invalid'}:
        raise ValueError('dag_valid_completed_work_is_immutable')
    for key in set(affected) & before.keys():
        if records[key]['attempts'] >= MAX_REWORK + 1:
            raise ValueError('dag_rework_limit')


def ready_order(plan, records, now):
    ordered = validate_plan(plan)
    depth = dict.fromkeys(ordered, 1)
    steps = {s.step_id: s for s in plan.steps}
    for key in reversed(ordered):
        for dependency in steps[key].depends_on:
            depth[dependency] = max(depth[dependency], depth[key] + 1)
    ready = [key for key in ordered if records[key]['state'] == 'queued'
             and all(records[d]['state'] == 'accepted' for d in steps[key].depends_on)]
    # Bounded static bonus + unbounded age: later high-priority arrivals cannot starve old work.
    def score(key):
        row = records[key]
        return (-(max(0, now - row['queued_at']) / 30 + row['priority'] * 4 + depth[key]),
                row['queued_at'], key)
    return tuple(sorted(ready, key=score))
