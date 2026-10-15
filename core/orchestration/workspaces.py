"""B47 persistent isolated copies. Only Integrator may publish their changed bytes."""
import json
import shutil
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from core.orchestration import workspace_io as io
from core.orchestration.resource_locks import ResourceLock


class WorkspaceManager:
    def __init__(self, root, *, owner, base_revision=None, protected_paths=()):
        self.root = Path(root).resolve()
        if not owner:
            raise ValueError('workspace_owner_required')
        self.owner = owner
        self.protected_paths = tuple(Path(p).resolve() for p in protected_paths)
        self.directory = self.root / '.work' / 'orchestration'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.locks = ResourceLock(self.directory / 'locks')
        self.state_path = self.directory / 'state.json'
        with self.exclusive():
            if not self.state_path.exists():
                io.save(self.state_path, dict(schema_version=1, frozen=False, owner=owner, workspaces={}))
        files = io.snapshot(self.root)
        self.base_revision = base_revision or io.revision(self.root, files)

    def exclusive(self):
        return self.locks.hold('workspace:' + str(self.root).casefold(), self.owner)

    def state(self):
        value = json.loads(self.state_path.read_text(encoding='utf-8'))
        if value['schema_version'] != 1:
            raise ValueError('workspace_schema_unsupported')
        return value

    def update(self, value):
        io.save(self.state_path, value)

    def get(self, workspace_id):
        try:
            return self.state()['workspaces'][workspace_id]
        except KeyError as exc:
            raise ValueError('workspace_unknown') from exc

    def path(self, workspace_id):
        self.get(workspace_id)
        return self.directory / workspace_id / 'source'

    def create(self, envelope, *, worker_id, allowed_paths):
        with self.exclusive():
            state = self.state()
            if state['frozen']:
                raise ValueError('workspace_collaboration_frozen')
            if any(r['envelope']['task_id'] == envelope.task_id
                   and r['envelope']['step_id'] == envelope.step_id for r in state['workspaces'].values()):
                raise ValueError('workspace_step_exists_use_original_worker')
            files = io.snapshot(self.root)
            base = io.revision(self.root, files)
            peers = [r for r in state['workspaces'].values()
                     if r['envelope']['task_id'] == envelope.task_id
                     and r['envelope']['base_revision'] == envelope.base_revision]
            if envelope.base_revision != self.base_revision or (not peers and base != self.base_revision):
                raise ValueError('workspace_base_conflict')
            if peers:
                files = io.unpack(json.loads((self.directory / peers[0]['id'] / 'base.json').read_text(
                    encoding='utf-8')))
                if io.hashes(files) != peers[0]['base_hashes']:
                    raise ValueError('workspace_base_snapshot_modified')
            allowed = tuple(allowed_paths)
            if not worker_id or not allowed:
                raise ValueError('workspace_write_scope_required')
            for name in allowed:
                self.check_writable(name)
            workspace_id = uuid4().hex
            home = self.directory / workspace_id
            home.mkdir()
            io.materialize(home / 'source', files)
            io.save(home / 'base.json', io.pack(files))
            record = dict(id=workspace_id, worker_id=worker_id, envelope=asdict(envelope),
                          allowed_paths=allowed, base_hashes=io.hashes(files),
                          dirty=peers[0]['dirty'] if peers else io.dirty(self.root),
                          status='ready', attempts=0, active=False, dependencies=[], artifact=None)
            state['workspaces'][workspace_id] = record
            self.update(state)
            return workspace_id

    @contextmanager
    def running(self, workspace_id):
        with self.locks.hold('running:' + workspace_id, self.owner):
            with self.exclusive():
                state = self.state()
                row = state['workspaces'][workspace_id]
                if state['frozen'] or row['active'] or row['status'] not in {'ready', 'rework'}:
                    raise ValueError('workspace_not_runnable')
                row.update(active=True, status='running')
                self.update(state)
            try:
                yield
            finally:
                with self.exclusive():
                    state = self.state()
                    row = state['workspaces'][workspace_id]
                    row['active'] = False
                    if row['status'] == 'running':
                        row['status'] = 'ready'
                    self.update(state)

    def check_scope(self, workspace_id, paths):
        row = self.get(workspace_id)
        for name in paths:
            io.safe_path(self.path(workspace_id), name)
            self.check_writable(name)
            if not any(name == root or name.startswith(root.rstrip('/') + '/')
                       for root in row['allowed_paths']):
                raise ValueError('workspace_changed_path_denied:' + name)

    def check_writable(self, name):
        from core.tools.permissions import hard_deny_reason
        path = io.safe_path(self.root, name)
        if hard_deny_reason(path, self.root, self.protected_paths) is not None:
            raise ValueError('workspace_hard_deny:' + name)

    def capture(self, workspace_id, *, changed_paths):
        with self.exclusive():
            state = self.state()
            row = state['workspaces'][workspace_id]
            if row['active'] or row['status'] not in {'ready', 'rework'}:
                raise ValueError('workspace_not_quiescent')
            files = io.snapshot(self.path(workspace_id))
            current = io.hashes(files)
            changed = sorted(k for k in current.keys() | row['base_hashes'].keys()
                             if current.get(k) != row['base_hashes'].get(k))
            self.check_scope(workspace_id, changed)
            if sorted(changed_paths) != changed:
                raise ValueError('workspace_changed_paths_mismatch')
            bundle = dict(workspace_id=workspace_id, envelope=row['envelope'],
                          changes={k: io.pack({k: files[k]})[k] if k in files else None for k in changed})
            artifact = self.directory / workspace_id / ('artifact-' + uuid4().hex + '.json')
            io.save(artifact, bundle)
            row.update(status='candidate', artifact=str(artifact), artifact_hash=io.digest(artifact.read_bytes()))
            self.update(state)
            return str(artifact)

    def rework(self, workspace_id, *, worker_id, envelope=None):
        with self.exclusive():
            state = self.state()
            row = state['workspaces'][workspace_id]
            if row['worker_id'] != worker_id or row['active'] or row['status'] != 'rejected':
                raise ValueError('workspace_rework_owner_or_state')
            if row['attempts'] >= 2:
                raise ValueError('workspace_rework_limit')
            if envelope is not None:
                before = row['envelope']
                if (any(getattr(envelope, k) != before[k] for k in ('task_id', 'step_id', 'base_revision'))
                        or envelope.plan_revision < before['plan_revision']
                        or envelope.owner_generation < before['owner_generation']):
                    raise ValueError('workspace_rework_identity_mismatch')
                paths = tuple(Path(p).resolve().relative_to(self.root).as_posix()
                              for p in envelope.write_scope)
                self.check_scope(workspace_id, paths)
                row['envelope'] = asdict(envelope)
            row.update(attempts=row['attempts'] + 1, status='rework')
            self.update(state)

    def retain_failure(self, workspace_id, reason):
        with self.exclusive():
            state = self.state()
            row = state['workspaces'][workspace_id]
            if row['active']:
                raise ValueError('workspace_not_quiescent')
            row.update(status='rejected', blocker=reason,
                       choices=['keep_user_changes', 'rework_original_worker', 'retain_artifact'])
            self.update(state)

    def freeze(self):
        with self.exclusive():
            state = self.state()
            state['frozen'] = True
            self.update(state)
            return state  # Includes owner, active operations and retained artifact references.

    def cleanup(self, workspace_id, *, processes_stopped):
        with self.locks.hold('running:' + workspace_id, self.owner), self.exclusive():
            state = self.state()
            row = state['workspaces'][workspace_id]
            if (processes_stopped is not True or row['active'] or row['dependencies']
                    or row['status'] != 'integrated'):
                raise ValueError('workspace_cleanup_requires_quiescent_integrated_state')
            path = self.directory / workspace_id / 'source'
            if path.resolve().parent != (self.directory / workspace_id).resolve():
                raise ValueError('workspace_cleanup_escape')
            shutil.rmtree(path)  # Artifacts, base, journals and persistent task facts stay intact.
            row['status'] = 'cleaned'
            self.update(state)
