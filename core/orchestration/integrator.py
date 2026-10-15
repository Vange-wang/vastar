"""Only publisher of B47 artifacts: verify versions, review immutable data, test, then CAS."""
import json
from pathlib import Path
from types import MappingProxyType
from uuid import uuid4

from core.orchestration import workspace_io as io


class Integrator:
    def __init__(self, manager):
        self.manager = manager

    def integrate(self, workspace_ids, *, envelopes, reviewer, validate):
        manager = self.manager
        with manager.exclusive():
            state = manager.state()
            if state['frozen']:
                raise ValueError('workspace_collaboration_frozen')
            rows = [state['workspaces'][key] for key in workspace_ids]
            journal = manager.directory / ('integration-' + uuid4().hex + '.json')
            try:
                base = io.snapshot(manager.root)
                ordered, changes = self._prepare(rows, envelopes, base)
                candidate = dict(base)
                for name, value in changes.items():
                    if value is None:
                        candidate.pop(name, None)
                    else:
                        candidate[name] = value
                view = MappingProxyType(candidate)
                if not callable(reviewer) or reviewer(view) is not True:
                    raise ValueError('workspace_independent_review_rejected')
                directory = manager.directory / ('integration-' + uuid4().hex)
                io.materialize(directory, candidate)
                if not callable(validate) or validate(directory) is not True:
                    raise ValueError('workspace_semantic_validation_failed')
                if io.snapshot(directory) != candidate:
                    raise ValueError('workspace_validator_modified_candidate')
                # Review/test callbacks cannot authorize stale contracts or external edits.
                self._prepare(rows, envelopes, io.snapshot(manager.root))
                if manager.state() != state:
                    raise ValueError('workspace_state_changed_during_validation')
                io.save(journal, dict(status='applying', workspaces=list(workspace_ids),
                                     before=io.pack(base), after=io.pack(candidate)))
                self._publish(base, changes, journal)
                sequence = max((r.get('publication_sequence', 0)
                                for r in state['workspaces'].values()), default=0) + 1
                for row in ordered:
                    row.update(status='integrated', published_hashes=io.hashes(candidate),
                               published_dirty=io.dirty(manager.root), publication_sequence=sequence)
                manager.update(state)
                io.save(journal, dict(status='integrated', workspaces=list(workspace_ids),
                                     before=io.pack(base), after=io.pack(candidate)))
                return dict(status='integrated', changed_paths=tuple(sorted(changes)), journal=str(journal))
            except Exception as exc:
                for row in rows:
                    if row['status'] == 'candidate':
                        row.update(status='rejected', blocker=str(exc),
                                   choices=['keep_user_changes', 'rework_original_worker', 'retain_artifact'])
                manager.update(state)
                if not journal.exists():
                    io.save(journal, dict(status='rejected', reason=str(exc), workspaces=list(workspace_ids)))
                raise

    def _prepare(self, rows, envelopes, base):
        from dataclasses import asdict

        manager = self.manager
        changed, ordered, steps = {}, [], set()
        if not rows or len({r['id'] for r in rows}) != len(rows):
            raise ValueError('workspace_batch_invalid')
        prior = self._prior(rows[0])
        required = {key for row in rows for key in row['envelope']['depends_on']}
        for row in prior:
            key = row['envelope']['step_id']
            if key in required:
                expected = dict(row['envelope'], plan_revision=rows[0]['envelope']['plan_revision'])
                if json.loads(json.dumps(asdict(envelopes(key)))) != expected:
                    raise ValueError('workspace_dependency_contract_conflict')
            steps.add(key)
        prior_paths = set()
        for row in prior:
            prior_paths.update(json.loads(Path(row['artifact']).read_text(encoding='utf-8'))['changes'])
        remaining = list(rows)
        while remaining:
            ready = [r for r in remaining if set(r['envelope']['depends_on']) <= steps]
            if not ready:
                raise ValueError('workspace_dependency_not_integrated')
            for row in ready:
                self._check_row(row, envelopes, base, asdict)
                artifact = Path(row['artifact'])
                bundle = json.loads(artifact.read_text(encoding='utf-8'))
                if bundle['envelope'] != row['envelope'] or bundle['workspace_id'] != row['id']:
                    raise ValueError('workspace_artifact_identity_mismatch')
                manager.check_scope(row['id'], bundle['changes'])
                for name, value in bundle['changes'].items():
                    if name in changed or name in prior_paths:
                        raise ValueError('workspace_same_file_conflict:' + name)
                    changed[name] = io.unpack({name: value})[name] if value is not None else None
                steps.add(row['envelope']['step_id'])
                ordered.append(row)
                remaining.remove(row)
        return ordered, changed

    def _prior(self, row):
        prior = sorted([r for r in self.manager.state()['workspaces'].values()
                if r['status'] in {'integrated', 'cleaned'}
                and all(r['envelope'][key] == row['envelope'][key]
                        for key in ('task_id', 'base_revision', 'owner_generation'))
                and r['envelope']['plan_revision'] <= row['envelope']['plan_revision']],
                      key=lambda r: r['publication_sequence'])
        for item in prior:
            artifact = Path(item['artifact'])
            if (artifact.parent != self.manager.directory / item['id']
                    or io.digest(artifact.read_bytes()) != item['artifact_hash']):
                raise ValueError('workspace_prior_artifact_modified')
        return prior

    def _check_row(self, row, envelopes, base, asdict):
        manager = self.manager
        if row['active'] or row['status'] != 'candidate':
            raise ValueError('workspace_candidate_not_quiescent')
        current = envelopes(row['envelope']['step_id'])
        if json.loads(json.dumps(asdict(current))) != row['envelope']:
            raise ValueError('workspace_contract_or_generation_conflict')
        prior = self._prior(row)
        expected_hashes = prior[-1]['published_hashes'] if prior else row['base_hashes']
        expected_dirty = prior[-1]['published_dirty'] if prior else row['dirty']
        git_changed = ((manager.root / '.git').exists()
                       and io.revision(manager.root, base) != manager.base_revision)
        if (row['envelope']['base_revision'] != manager.base_revision or git_changed
                or io.hashes(base) != expected_hashes or io.dirty(manager.root) != expected_dirty):
            raise ValueError('workspace_main_or_base_conflict')
        artifact = Path(row['artifact'])
        if (artifact.parent != manager.directory / row['id']
                or io.digest(artifact.read_bytes()) != row['artifact_hash']):
            raise ValueError('workspace_artifact_modified')

    def _publish(self, base, changes, journal):
        applied = []
        try:
            for name, value in changes.items():
                io.replace_file(self.manager.root, name, base.get(name), value)
                applied.append(name)
        except Exception as exc:
            conflicts = []
            for name in reversed(applied):
                try:
                    io.replace_file(self.manager.root, name, changes[name], base.get(name))
                except Exception:
                    conflicts.append(name)  # Never overwrite a user's intervening edit.
            data = json.loads(journal.read_text(encoding='utf-8'))
            data.update(status='rollback_conflict' if conflicts else 'rolled_back',
                        reason=str(exc), retained_user_paths=conflicts)
            io.save(journal, data)
            raise


def require_integrated_result(envelope, result):
    """A host acceptance callback cannot skip deterministic publication for write steps."""
    if not envelope.write_scope:
        return
    refs = [ref[10:] for ref in result['artifact_refs'] if ref.startswith('workspace:')]
    if len(refs) != 1:
        raise ValueError('workspace_integration_receipt_required')
    artifact = Path(refs[0])
    state = json.loads((artifact.parent.parent / 'state.json').read_text(encoding='utf-8'))
    bundle = json.loads(artifact.read_text(encoding='utf-8'))
    row = state['workspaces'][bundle['workspace_id']]
    from dataclasses import asdict
    changed = {str(artifact.parents[3] / name) for name in bundle['changes']}
    if (row['status'] not in {'integrated', 'cleaned'} or row['artifact'] != str(artifact)
            or io.digest(artifact.read_bytes()) != row['artifact_hash']
            or row['envelope'] != json.loads(json.dumps(asdict(envelope)))
            or changed != set(result['changed_paths'])):
        raise ValueError('workspace_integration_not_verified')
