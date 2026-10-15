"""事实仓储：读写均由 MemoryService 串行事务持锁，索引是同事务投影。"""
import json


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class FactRepository:
    def memory_revision(self):
        with self._lock:
            return self._conn.execute('SELECT COALESCE(MAX(revision),0) FROM memory_revisions').fetchone()[0]

    def memory_item(self, scope, key):
        with self._lock:
            row = self._conn.execute('SELECT record_json,status FROM memory_items WHERE scope=? AND key=?',
                                     (scope, key)).fetchone()
            return {**json.loads(row[0]), 'status': row[1]} if row else None

    def memory_publish(self, item):
        self._conn.execute('INSERT INTO memory_items VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                           'text=excluded.text,revision=excluded.revision,status=excluded.status,record_json=excluded.record_json',
                           (item['id'], item['scope'], item['kind'], item['key'], item['text'],
                            item['revision'], item['status'], encode(item)))
        self._conn.execute('INSERT INTO memory_index VALUES(?,?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET '
                           'text=excluded.text,revision=excluded.revision',
                           (item['id'], item['scope'], item['kind'], item['text'], item['revision']))

    def memory_proposal(self, proposal_id, session_id, record, receipt):
        self._conn.execute('INSERT INTO memory_proposals VALUES(?,?,?,?,?,?)',
                           (proposal_id, session_id, receipt['status'], encode(record), encode(receipt), self._now()))

    def memory_current(self, scopes):
        with self._lock:
            rows = self._conn.execute('SELECT m.record_json FROM memory_index i JOIN memory_items m ON m.id=i.item_id '
                                     "WHERE i.scope IN (" + ','.join('?' for _ in scopes) + ") AND m.status='active' "
                                     'AND i.revision=m.revision ORDER BY m.revision', tuple(scopes)).fetchall()
            return [json.loads(r[0]) for r in rows]
