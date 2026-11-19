"""User-command proposals narrowed by the accepted Task and current host authority."""
import hashlib
from pathlib import Path

from core.orchestration.context import ContextStore, Evidence
from core.orchestration.contracts import (
    Budget,
    Permissions,
    PlanRevision,
    TaskEnvelope,
    WorkerProfile,
)
from core.tools.permissions import hard_deny_reason, is_env_path, resolve_read_path


def proposal(raw, config, tools, task, step):
    if type(raw) is not dict or set(raw) != {'children'}:
        raise ValueError('team_host_limits_only: only children may be proposed')
    items = raw['children']
    if type(items) is not list or not 1 <= len(items) <= config.max_workers:
        raise ValueError(f'team_child_limit:{config.max_workers}')
    root = tools.project_root.resolve()
    accepted = {str(Path(c['path']).resolve()): c['sha256'] for key in step['acceptance_ids']
                if isinstance(c := task['acceptance'][key], dict) and c.get('kind') == 'file_hash'}
    result = []
    for item in items:
        if type(item) is not dict or set(item) - {'goal', 'read_paths', 'required_facts', 'write_path'}:
            raise ValueError('team_child_schema: model cannot set budgets, tools, depth or owners')
        if not isinstance(item.get('goal'), str) or not item['goal'].strip():
            raise ValueError('team_goal_required')
        reads = _reads(item.get('read_paths'), tools)
        facts = item.get('required_facts')
        if (type(facts) is not list or not facts or any(type(f) is not str or not f.strip() for f in facts)
                or any(not any(f in data for _, data in reads) for f in facts)):
            raise ValueError('team_source_facts_required')
        write = item.get('write_path')
        if write is not None:
            path = resolve_read_path(root, write)
            if (not path.is_relative_to(root) or str(path) not in accepted
                    or hard_deny_reason(path, root, tools.protected_paths) or is_env_path(path)):
                raise ValueError('team_write_not_authorized_by_task')
            if tools.permission_mode != 'auto':
                raise ValueError('team_write_requires_host_auto_permission; read-only remains available')
            write = str(path)
        result.append(dict(goal=item['goal'], reads=reads, facts=tuple(facts), write=write,
                           sha256=accepted.get(write), output=Path(write).relative_to(root).as_posix() if write else None))
    return result


def _reads(paths, tools):
    if type(paths) is not list or not paths or len(paths) > 10:
        raise ValueError('team_explicit_read_paths_required')
    result = []
    for raw in paths:
        path = resolve_read_path(tools.project_root, raw)
        if (not path.is_relative_to(tools.project_root.resolve()) or is_env_path(path)
                or any(path.is_relative_to(Path(p).resolve()) for p in tools.protected_paths)):
            raise ValueError('team_read_outside_host_scope')
        if not tools.registry.is_enabled('read_file') or path.stat().st_size > 65536:
            raise ValueError('team_read_disabled_or_source_too_large')
        result.append((str(path), path.read_text(encoding='utf-8')))
    return result


def build_plan(items, task, step, config, limit, base_revision):
    evidence, envelopes = [], []
    tools = ('read_file', 'write_file') if any(i['write'] for i in items) else ('read_file',)
    permission = Permissions(tools, tuple(dict.fromkeys(p for i in items for p, _ in i['reads'])),
                             tuple(dict.fromkeys(i['write'] for i in items if i['write'])))
    child_budget = Budget(min(3, limit.max_calls), min(20000, limit.max_estimated_tokens),
                          limit.deadline_seconds, limit.max_cost_yuan / config.max_workers,
                          min(limit.deadline_seconds, limit.max_active_seconds / config.max_workers))
    for index, item in enumerate(items):
        refs = []
        for n, (path, _text) in enumerate(item['reads']):
            ref = f'source-{index + 1}-{n + 1}'
            digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            evidence.append(Evidence(ref, f'path={path}\nsha256={digest}', digest))
            refs.append(ref)
        write = (item['write'],) if item['write'] else ()
        constraints = tuple(task['constraints']) + (
            'Read the supplied paths with read_file. Preserve these facts: ' + repr(item['facts']),
            'Read paths: ' + repr([p for p, _ in item['reads']]),
            'Write target relative to isolated workspace: ' + str(item['output']),
            'Write only the write_scope in an isolated workspace; use relative filenames there.',)
        envelopes.append(TaskEnvelope(task['task_id'], f"{step['step_id']}-child-{index + 1}",
            step['step_id'], item['goal'], base_revision, 1, 1, child_budget,
            tuple(item['facts']), input_refs=tuple(refs), allowed_tools=tools if write else ('read_file',),
            read_scope=tuple(p for p, _ in item['reads']), write_scope=write, constraints=constraints))
    profile = WorkerProfile('host-entry', 'worker', ('stream',), permission, child_budget,
                            delegation_depth=config.max_depth)
    return PlanRevision(task['task_id'], 1, tuple(envelopes)), profile, ContextStore(tuple(evidence))
