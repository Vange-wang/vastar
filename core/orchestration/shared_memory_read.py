"""Versioned shared reads with present-day revocation and conflict checks."""
import json
import re

from memory import policy

CITATION = re.compile(r'(mem-[0-9a-f]+)@r(\d+)')


class SharedMemoryRead:
    def _item_by_id(self, item_id):
        row = self.store._conn.execute('SELECT record_json,status FROM memory_items WHERE id=?',
                                       (item_id,)).fetchone()
        return {**json.loads(row['record_json']), 'status': row['status']} if row else None

    def _items(self, scopes):
        if not scopes:
            return []
        rows = self.store._conn.execute(
            'SELECT record_json,status FROM memory_items WHERE scope IN ('
            + ','.join('?' for _ in scopes) + ')', tuple(scopes)).fetchall()
        return [{**json.loads(row['record_json']), 'status': row['status']} for row in rows]

    def _conflicted(self, item):
        pending, seen = [item], set()
        while pending:
            current = pending.pop()
            identity = (current['id'], current['revision'])
            if identity in seen:
                continue
            seen.add(identity)
            if any(c['key'] == current['key'] and c['winner']['revision'] == current['revision']
                   for c in self.conflicts(scope=current['scope'])):
                return True
            for ref in current.get('citations', ()):
                match = CITATION.fullmatch(ref)
                source = self._item_by_id(match.group(1)) if match else None
                if (source is None or not policy.active(source, self._now())
                        or source['revision'] != int(match.group(2))):
                    return True
                pending.append(source)
        return False

    def snapshot(self, scopes, *, at_revision=None):
        with self.store._lock:
            current = self.store.memory_revision()
            pinned = current if at_revision is None else at_revision
            if type(pinned) is not int or pinned < 0 or pinned > current:
                raise ValueError('memory_snapshot_revision_invalid')
            live = {i['id']: i for i in self._items(scopes) if policy.active(i, self._now())}
            versions = {}
            for row in self.store._conn.execute('SELECT item_json FROM memory_revisions '
                    'WHERE revision<=? AND item_json IS NOT NULL AND deleted=0 ORDER BY revision',
                    (pinned,)):
                item = json.loads(row[0])
                if item['id'] in live and policy.active(item, self._now()):
                    versions[item['id']] = item
            conflicts = [c for c in self.conflicts() if c['scope'] in scopes and any(
                i['scope'] == c['scope'] and i['key'] == c['key']
                and i['revision'] == c['winner']['revision'] for i in live.values())]
            return dict(items=list(versions.values()), pinned_revision=pinned,
                        memory_revision=current,
                        stale=current > pinned or any(self._conflicted(i) for i in live.values()),
                        conflicts=conflicts, note='Historical versions filtered by current revocations')

    def latest(self, scopes):
        return self.snapshot(scopes)

    def revalidate(self, refs):
        report = {'current': [], 'stale': [], 'revoked': [], 'unknown': []}
        with self.store._lock:
            for ref in refs:
                match = CITATION.fullmatch(ref)
                item = self._item_by_id(match.group(1)) if match else None
                if item is None:
                    state = 'unknown'
                elif not policy.active(item, self._now()):
                    state = 'revoked'
                elif item['revision'] != int(match.group(2)) or self._conflicted(item):
                    state = 'stale'
                else:
                    state = 'current'
                report[state].append(ref)
        return report
