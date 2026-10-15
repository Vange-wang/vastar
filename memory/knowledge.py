"""S09 orchestration: candidates are not authority; only explicit UI confirmation commits."""
import hashlib
import uuid

from memory import policy
from memory.knowledge_codec import FORMAT, decode, encode, reference
from memory.knowledge_sources import KnowledgeSources


class ProjectKnowledge:
    def __init__(self, memory, project_root, cwd=None):
        self.memory = memory
        self.sources = KnowledgeSources(memory, project_root, cwd) if memory else None
        self._previews = {}
        if memory:
            memory.knowledge = self
            memory.store._knowledge_guard = self.refresh
            memory.store._knowledge_clear = self._previews.clear

    def require_memory(self):
        if self.memory is None:
            raise ValueError('dependency_missing：依赖缺失（MemoryService），未归档')
        return self.memory

    def preview(self, session_id, selection):
        memory = self.require_memory()
        self.refresh()
        source, entry = self.sources.select(selection)
        scope = entry.get('scope', 'project:' + memory.project_id)
        policy.scope_for(memory, session_id, scope)
        if scope != 'project:' + memory.project_id:
            raise ValueError('knowledge_scope_denied')
        old = memory.store.memory_item(scope, entry['key'])
        payload = {'format': FORMAT, 'project_id': memory.project_id, 'scope': scope, 'key': entry['key'],
                   'state': entry['state'], 'conclusion': entry['text'], 'source': source,
                   'supersedes': reference(memory.project_id, old) if old else None}
        # Identical evidence is a duplicate, not a replacement of itself.
        prior = decode(old['text']) if old else None
        if prior and not self.stale(old) and {**payload, 'supersedes': prior['supersedes']} == prior:
            payload = prior
        proposal = {'key': entry['key'], 'kind': 'fact', 'text': encode(payload), 'scope': scope,
                    'validity': entry.get('validity', {}), 'expected_revision': old['revision'] if old else 0}
        if old and old['text'] != proposal['text']:
            proposal['replace'] = True
        eligible, reason = entry['state'] == 'effective', ''
        try:
            policy.validate(proposal)
            if policy.sensitive(encode(proposal)):
                raise ValueError('sensitive_credentials')
        except ValueError as exc:
            eligible, reason = False, str(exc)
        pid = hashlib.sha256(encode([session_id, proposal]).encode()).hexdigest()
        self._previews[pid] = (session_id, {'redacted': True} if reason == 'sensitive_credentials' else proposal)
        return {'status': 'preview', 'state': entry['state'], 'eligible': eligible, 'reason': reason,
                'preview_id': pid, 'proposal': proposal if reason != 'sensitive_credentials' else {'redacted': True},
                'stages': ['extract', 'dedup_conflict'], 'conflict': bool(proposal.get('replace')),
                'notice': '尚未归档；确认仅授权保存此事实，不授权任何工程操作。'}

    def commit(self, session_id, preview_id, *, user_confirmed=False, origin='user'):
        memory = self.require_memory()
        if not user_confirmed:
            raise ValueError('archive_requires_current_user_confirmation')
        saved = self._previews.get(preview_id)
        if saved is None or saved[0] != session_id:
            raise ValueError('archive_preview_missing_or_wrong_session')
        proposal = dict(saved[1])
        if proposal.get('redacted'):
            raise ValueError('sensitive_credentials')
        self.refresh()
        payload = decode(proposal['text'])
        self.sources.verify(payload)
        if policy.sensitive(encode(proposal)):
            raise ValueError('sensitive_credentials')
        # This is the trusted human command handler, not a callable model write tool.
        mid = 'archive-user-' + uuid.uuid4().hex
        with memory.store.transaction():
            memory.store.append_message(mid, session_id, role='user', content='/remember ' + encode(proposal), origin=origin)
            proposal['source_refs'] = [f'history:{session_id}/{mid}@r1']
            receipt = memory.submit(session_id, proposal, input_message_id=mid)
        stages = ['extract', 'dedup_conflict', 'submit']
        if receipt['status'] not in {'committed', 'duplicate'}:
            return {'status': receipt['status'], 'receipt': receipt, 'stages': stages}
        item = receipt['item']
        found = self.find(session_id, key=item['key'])['items']
        stages.append('retrieval_verify')
        if not any(i['reference'] == reference(memory.project_id, item) for i in found):
            return {'status': 'verification_failed', 'receipt': receipt, 'stages': stages}
        return {'status': 'archived', 'reference': reference(memory.project_id, item),
                'receipt': receipt, 'stages': [*stages, 'deliver_reference']}

    def stale(self, item):
        return self.memory.store._conn.execute('SELECT 1 FROM memory_proposals WHERE id=?',
                    (f"knowledge-stale:{item['id']}:{item['revision']}",)).fetchone() is not None

    def refresh(self):
        memory = self.require_memory()
        with memory.store._lock:
            items = memory.store.memory_current(['project:' + memory.project_id])
        for item in items:
            payload = decode(item['text'])
            if payload is None or self.stale(item):
                continue
            try:
                if not policy.active(item, memory.store._now()):
                    raise ValueError('knowledge_expired')
                self.sources.verify(payload)
            except ValueError as exc:
                self._invalidate(item, str(exc))

    def invalidate_replaced(self, old, new_refs):
        if decode(old['text']) is not None:
            from memory.forget_plan import source_id
            self._invalidate(old, 'knowledge_superseded', protected={source_id(ref) for ref in new_refs})

    def _invalidate(self, item, reason, *, protected=frozenset()):
        from memory.forget_buffers import clear_caches
        from memory.forget_cleanup import clean_snapshots
        from memory.forget_plan import plan
        store = self.memory.store
        with store.transaction():
            if self.stale(item):
                return
            impact = plan(store, item)
            entry = {'action_id': f"knowledge-stale:{item['id']}:{item['revision']}", 'impact': impact}
            clean_snapshots(store._conn, entry, False)
            clear_caches(store, entry)
            for sid in set(impact['sources']) - protected:
                store._conn.execute("UPDATE history_sources SET status='obsolete' WHERE source_id=?", (sid,))
            store.memory_proposal(entry['action_id'], None, {'item_id': item['id'], 'revision': item['revision']},
                                  {'status': 'needs_revalidation', 'reason': reason, 'knowledge_state': 'obsolete'})

    def find(self, session_id, *, query='', key=None):
        memory = self.require_memory()
        result = memory.list(session_id)
        items = []
        for item in result['items']:
            payload = decode(item['text'])
            if payload and (key is None or item['key'] == key) and query.casefold() in payload['conclusion'].casefold():
                items.append({'reference': reference(memory.project_id, item), **payload})
        return {'status': 'ok' if items else 'empty', 'items': items,
                'notice': '归档事实不是执行授权；只返回当前有效且可核实来源的结论。'}

    def open(self, session_id, ref):
        memory = self.require_memory()
        for item in memory.list(session_id)['items']:
            payload = decode(item['text'])
            if payload and reference(memory.project_id, item) == ref:
                return self.sources.open(payload)
        raise ValueError('archive_reference_missing_stale_or_denied')

    def history(self, session_id, key):
        memory = self.require_memory()
        self.refresh()
        current = memory.store.memory_item('project:' + memory.project_id, key)
        if current is None or current['status'] != 'active':
            return []  # withdrawals/deletions must not reappear via a history shortcut
        rows = memory.store._conn.execute('SELECT item_json FROM memory_revisions WHERE key=? ORDER BY revision', (current['id'],))
        result = []
        import json
        for row in rows:
            item = json.loads(row[0]) if row[0] else None
            payload = decode(item['text']) if item else None
            if payload is not None:
                obsolete = item['revision'] != current['revision'] or self.stale(item) or not policy.active(item, memory.store._now())
                result.append({**payload, 'state': 'obsolete' if obsolete else payload['state'],
                               'reference': reference(memory.project_id, item), 'notice': '历史核对，不是最新指令或工程授权'})
        return result
