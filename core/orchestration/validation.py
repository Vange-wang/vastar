"""Strict C08 result boundary. A model's completion claim is never acceptance."""
import json
from dataclasses import fields, replace

from core.orchestration.contracts import TaskResult
from core.orchestration.permissions import canonical, within


def thin_summary(text):
    return text if len(text) <= 512 else text[:480] + '…（完整内容按需 read_report）'


def terminal(envelope, status, reason, usage=None, summary=''):
    return TaskResult(status, thin_summary(summary), (), (), (envelope.goal,), (reason,), (),
                      envelope.base_revision, envelope.plan_revision,
                      envelope.owner_generation, usage)


def validate_result(text, envelope, effective, store, allowed_refs, accept, usage):
    try:
        raw = json.loads(text)
        required = {f.name for f in fields(TaskResult)} - {'actual_usage'}
        if type(raw) is not dict or set(raw) != required:
            raise ValueError('worker_result_schema_invalid')
        for key in ('artifact_refs', 'evidence_refs', 'remaining_work', 'blockers', 'changed_paths'):
            if type(raw[key]) is not list:
                raise ValueError('worker_result_list_required')
            raw[key] = tuple(raw[key])
        result = TaskResult(**raw, actual_usage=usage)
        if any(getattr(result, key) != getattr(envelope, key)
               for key in ('base_revision', 'plan_revision', 'owner_generation')):
            raise ValueError('worker_result_stale')
        if any(not within(canonical(path), effective.write_scope) for path in result.changed_paths):
            raise ValueError('worker_result_changed_path_denied')
        for ref in result.evidence_refs + result.artifact_refs:
            item = store.get(ref, allowed_refs, effective.memory_scope)
            if ref in result.artifact_refs and item.kind != 'artifact':
                raise ValueError('worker_result_artifact_required')
        if result.status == 'completed':
            if (not result.evidence_refs or result.remaining_work or result.blockers
                    or not result.summary.strip()):
                raise ValueError('worker_completion_evidence_missing')
            if accept is None or accept(envelope, result) is not True:
                raise ValueError('worker_acceptance_not_verified')
        return replace(result, summary=thin_summary(result.summary))
    except (ValueError, TypeError, KeyError) as exc:
        return terminal(envelope, 'incomplete', str(exc), usage, text)


def with_runtime_status(result, runtime):
    if runtime.status.lower() != 'completed' or runtime.partial or runtime.errors:
        status = 'cancelled' if runtime.status.lower() == 'cancelled' else 'incomplete'
        return replace(result, status=status,
                       blockers=result.blockers + ('worker_runtime_not_complete',))
    return result
