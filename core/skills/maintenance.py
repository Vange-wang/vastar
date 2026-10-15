"""S10 service: only trusted user commands mutate releases; model tools cannot publish."""
import copy

from core.skills import release_sources as sources
from core.skills import release_store as journal
from core.skills import release_view
from core.skills.release_package import digest, generalize, require, validate
from core.tools.operation_receipt import ToolOutcome
from memory.repositories.tasks import encode

SKILL = 'skill-maintenance'


class Maintenance:
    def __init__(self, context):
        self.context = context
        self.store = context.operation_ledger.store
        self.project = context.task_service.project_id
        self.base = copy.deepcopy(context.skills._discovery)
        self.builtin_refs = {}

    def candidate(self, session, *, source_task, memory_key, bundle, private_terms):
        task = sources.accepted(self, source_task)
        item = sources.procedure(self, session, memory_key)
        safe = generalize(bundle, private_terms)
        meta = validate(safe, self.context.registry)
        rid = meta.name + '@' + meta.version
        require(rid not in journal.catalog(self)[0], 'version_conflict')
        artifact = self.context.artifacts.put(encode(safe).encode(), source_ref=item['id'] + '@' + str(item['revision']))
        row = {'id': rid, 'name': meta.name, 'version': meta.version, 'hash': digest(safe),
               'artifact': artifact.uri, 'state': 'candidate', 'author_session': session,
               'author_task': (self.store.task_for_session(session) or {}).get('task_id'),
               'source_task': source_task, 'source_hash': digest(task), 'memory_key': memory_key,
               'memory_id': item['id'], 'memory_revision': item['revision'], 'validation': None}
        journal.append(self, 'candidate', {'release': row})
        return row

    def validate(self, session, *, release_id, change_id):
        row = self.row(release_id)
        require(row['state'] == 'candidate', 'candidate_required')
        sources.current(self, row)
        row['validation'] = sources.independent(self, row, session, change_id)
        row['state'] = 'validated'
        journal.append(self, 'validate', {'release': row})
        return row

    def publish(self, session, *, release_id, expected_head, rollback=False):
        if rollback:
            sources.quiescent(self)
        row = self.row(release_id)
        require(row['state'] == ('retired' if rollback else 'validated'), 'validated_or_retired_required')
        old = journal.catalog(self)[1].get(row['name'])
        require(old == expected_head, 'release_head_conflict')
        proof = row['validation']
        require(proof is not None, 'independent_validation_required')
        fresh = sources.independent(self, row, proof['session'], proof['change_id'])
        require(fresh == proof, 'validation_changed_revalidate_required')
        release_view.compatible(self, row)
        release_view.freeze_existing(self)
        row['state'] = 'enabled'
        journal.append(self, 'rollback' if rollback else 'enable', {'release': row, 'actor_session': session})
        return row

    def retire(self, session, *, release_id):
        sources.quiescent(self)
        row = self.row(release_id)
        require(row['state'] == 'enabled', 'enabled_required')
        release_view.freeze_existing(self)
        row['state'] = 'retired'
        journal.append(self, 'retire', {'release': row, 'actor_session': session})
        return row

    def row(self, release_id):
        row = journal.catalog(self)[0].get(release_id)
        require(row is not None, 'release_missing')
        return copy.deepcopy(row)

    def status(self):
        versions, heads = journal.catalog(self)
        return {'heads': heads, 'releases': list(versions.values()), 'notice': 'Lifecycle enabled does not change skills.enabled or tool permissions.'}

    def snapshot(self, task_id=None):
        return release_view.view(self, task_id)

    def check_live(self, meta, visited=None):
        visited = set() if visited is None else visited
        require(meta.name not in visited, 'dependency_cycle')
        visited.add(meta.name)
        for tool in meta.required_tools:
            spec = self.context.registry.get(tool)
            require(spec and spec.effect == 'read' and self.context.registry.is_enabled(tool), 'dependency_tool_unavailable')
        for row in journal.catalog(self)[0].values():
            if row['hash'] == meta.sha256:
                _, deps = release_view.metadata(self, row)
                versions = journal.catalog(self)[0].values()
                for name, expected in deps.items():
                    require(name in self.context.skills._enabled, 'dependency_disabled')
                    dep = next((r for r in versions if r['name'] == name and r['hash'] == expected), None)
                    target = release_view.metadata(self, dep)[0] if dep else self.base.available.get(name)
                    require(target and target.enabled and target.sha256 == expected, 'dependency_missing_or_changed')
                    self.check_live(target, set(visited))
                return


def guard(context, spec):
    skills = context.skills
    if skills and SKILL in skills.active_names() and spec.effect != 'read' and spec.name not in {'terminate_process', 'ask_task_question'}:
        return ToolOutcome(spec.name, 'rejected', 'S10 prepares data; publication and experience writes require user commands', origin='maintenance_scope_gate')
    return None


def install(context, coordinator):
    if getattr(context, 'skills', None) is None or getattr(context, 'operation_ledger', None) is None:
        return
    service = Maintenance(context)
    coordinator.skill_maintenance = service
    context.skills.bind_releases(service)
