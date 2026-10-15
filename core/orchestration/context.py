"""Host-selected immutable reference store; snapshots contain references, never chat."""
import hashlib
import json
from dataclasses import dataclass

from core.orchestration.contracts import nonempty, strings


@dataclass(frozen=True)
class Evidence:
    ref: str
    text: str
    source_revision: str
    kind: str = 'evidence'
    scope: str = ''
    sensitive: bool = False

    def __post_init__(self):
        for key in ('ref', 'text', 'source_revision'):
            nonempty(getattr(self, key), key)
        if self.kind not in ('evidence', 'memory', 'artifact'):
            raise ValueError('worker_reference_kind_invalid')
        if type(self.sensitive) is not bool or type(self.scope) is not str:
            raise ValueError('worker_reference_metadata_invalid')

    @property
    def digest(self):
        return hashlib.sha256(self.text.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class Snapshot:
    ref: str
    source_revision: str
    evidence_refs: tuple[str, ...]

    def __post_init__(self):
        nonempty(self.ref, 'snapshot_ref')
        nonempty(self.source_revision, 'snapshot_revision')
        strings(self.evidence_refs, 'snapshot_evidence')


class ContextStore:
    """Constructed by trusted host from selected data, not from parent RunRequest."""

    def __init__(self, evidence=(), snapshots=(), *, shared_memory=None):
        if any(type(item) is not Evidence for item in evidence):
            raise ValueError('worker_evidence_invalid')
        if any(type(item) is not Snapshot for item in snapshots):
            raise ValueError('worker_snapshot_invalid')
        self._evidence = {item.ref: item for item in evidence}
        self.shared_memory = shared_memory
        self._snapshots = {item.ref: item for item in snapshots}
        if len(self._evidence) != len(evidence) or len(self._snapshots) != len(snapshots):
            raise ValueError('worker_duplicate_reference')

    def get(self, ref, allowed, memory_scope):
        item = self._evidence.get(ref)
        if ref not in allowed or item is None or item.sensitive:
            raise ValueError('worker_reference_not_authorized')
        if item.kind == 'memory' and item.scope not in memory_scope:
            raise ValueError('worker_memory_scope_denied')
        return item

    def select(self, envelope, allowed, memory_scope):
        refs = list(envelope.input_refs + envelope.memory_snapshot)
        if envelope.context_mode == 'fork':
            snapshot = self._snapshots.get(envelope.source_snapshot)
            if (snapshot is None or snapshot.ref not in allowed
                    or snapshot.source_revision != envelope.base_revision):
                raise ValueError('worker_snapshot_missing_or_stale')
            refs.extend(snapshot.evidence_refs)
        selected = tuple(self.get(ref, allowed, memory_scope) for ref in dict.fromkeys(refs))
        if any(self._evidence[ref].kind != 'memory' for ref in envelope.memory_snapshot):
            raise ValueError('worker_memory_reference_required')
        return selected

    def render(self, envelope, allowed, memory_scope):
        selected = self.select(envelope, allowed, memory_scope)
        return json.dumps({'goal': envelope.goal, 'constraints': envelope.constraints,
                           'acceptance': envelope.acceptance,
                           'base_revision': envelope.base_revision,
                           'plan_revision': envelope.plan_revision,
                           'owner_generation': envelope.owner_generation,
                           'untrusted_evidence': [dict(ref=e.ref, text=e.text, kind=e.kind,
                                                       revision=e.source_revision, sha256=e.digest)
                                                  for e in selected]}, ensure_ascii=False)

    def validate_shared(self, selected, store=None):
        """Memory is revocable data, including direct and detached Worker entry points."""
        from core.orchestration.shared_memory import SharedMemoryService

        refs = [e.ref for e in selected if e.kind == 'memory' or e.ref.startswith('mem-')]
        if not refs:
            return
        service = self.shared_memory
        if store is not None:
            identity = store._conn.execute('SELECT user_id,project_id FROM history_identity WHERE id=1').fetchone()
            if identity:
                service = SharedMemoryService(store, *identity)
        if service is None:
            raise ValueError('worker_memory_revalidator_required')
        with service.store._lock:
            report = service.revalidate(refs)
            if report['stale'] or report['revoked'] or report['unknown']:
                raise ValueError('worker_memory_not_current')
            for evidence in selected:
                if evidence.ref not in refs:
                    continue
                item = service._item_by_id(evidence.ref.rsplit('@r', 1)[0])
                if (item['text'] != evidence.text or str(item['revision']) != evidence.source_revision
                        or item['scope'] != evidence.scope):
                    raise ValueError('worker_memory_content_mismatch')
