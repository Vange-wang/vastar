"""Task-wide immutable selection; revocation/dependency faults never fall back silently."""
import copy
from dataclasses import asdict

from core.skills import release_sources as sources
from core.skills import release_store as journal
from core.skills.registry import Discovery, SkillFault, SkillMeta
from core.skills.release_package import load, materialize, require
from memory.repositories.tasks import encode


def metadata(service, row):
    sources.current(service, row)
    bundle = load(service.context.artifacts, row['artifact'])
    meta = materialize(bundle, service.context.registry, row['artifact'])
    require(meta.sha256 == row['hash'], 'release_artifact_changed')
    return meta, bundle['dependencies']


def resolve(service, selection):
    versions, _ = journal.catalog(service)
    result, dependencies = Discovery(), {}
    for name, selected in selection.items():
        try:
            if selected.get('release'):
                meta, deps = metadata(service, versions[selected['release']])
            else:
                meta = SkillMeta(**load(service.context.artifacts, selected['artifact']))
                deps = {}
                for tool in meta.required_tools:
                    spec = service.context.registry.get(tool)
                    require(spec and spec.effect == 'read' and service.context.registry.is_enabled(tool), 'dependency_tool_unavailable')
            result.available[name], dependencies[name] = meta, deps
        except Exception as exc:
            result.unavailable.append(SkillFault(name, 'release_unavailable:' + str(exc)))
    while True:
        bad = [name for name, deps in dependencies.items() if name in result.available and any(
            dep not in result.available or result.available[dep].sha256 != digest
            or not result.available[dep].enabled or dep not in service.context.skills._enabled for dep, digest in deps.items())]
        if not bad:
            break
        for name in bad:
            result.available.pop(name)
            result.unavailable.append(SkillFault(name, 'dependency_missing_or_changed'))
    return result


def selection(service):
    versions, heads = journal.catalog(service)
    chosen = {}
    for name, meta in service.base.available.items():
        if name not in heads:
            if name not in service.builtin_refs:
                artifact = service.context.artifacts.put(encode(asdict(meta)).encode(), source_ref='builtin-skill:' + name)
                service.builtin_refs[name] = artifact.uri
            chosen[name] = {'artifact': service.builtin_refs[name]}
    for name, rid in heads.items():
        if rid:
            chosen[name] = {'release': rid}
        else:
            # A retired name must not silently rediscover an older file-system version.
            chosen[name] = {'release': 'retired:' + name}
    return chosen


def view(service, task_id=None):
    with service.store._lock:
        if task_id:
            task = service.store.task_get(task_id)
            require(task and task['project_id'] == service.project, 'task_scope_denied')
            chosen = journal.pins(service, task_id)
            if chosen is None:
                chosen = selection(service)
                journal.append(service, 'pin', {'task_id': task_id, 'pins': chosen}, tool='skill_pin')
        else:
            chosen = selection(service)
        result = resolve(service, chosen)
        result.unavailable.extend(copy.deepcopy(service.base.unavailable))
        return result


def freeze_existing(service):
    rows = service.store._conn.execute("SELECT task_id FROM tasks WHERE project_id=? AND status NOT IN ('COMPLETED','CANCELLED')", (service.project,)).fetchall()
    for row in rows:
        view(service, row[0])


def compatible(service, candidate):
    _, heads = journal.catalog(service)
    available = view(service).available
    meta, deps = metadata(service, candidate)
    for name, expected in deps.items():
        require(name in available and available[name].sha256 == expected and available[name].enabled
                and name in service.context.skills._enabled, 'dependency_missing_or_changed')
    previous = available.get(candidate['name'])
    if previous is None:
        versions, _ = journal.catalog(service)
        prior = [r for r in versions.values() if r['name'] == candidate['name'] and r['validation'] is not None
                 and r['id'] != candidate['id']]
        if prior:
            previous, _ = metadata(service, prior[-1])
    if previous is not None:
        require(set(meta.permissions) <= set(previous.permissions)
                and set(meta.required_tools) <= set(previous.required_tools)
                and meta.needs_memory == previous.needs_memory, 'incompatible_permission_or_dependency_change')
    # Cycles are rejected even when hashes happen to be pinned correctly.
    versions, _ = journal.catalog(service)
    pending, visited = list(deps), {candidate['name']}
    while pending:
        name = pending.pop()
        require(name != candidate['name'], 'dependency_cycle')
        if name in visited:
            continue
        visited.add(name)
        rid = heads.get(name)
        if rid:
            pending.extend(load(service.context.artifacts, versions[rid]['artifact'])['dependencies'])
    return meta
