"""Opt-in B44 child-plan snapshots, inside the existing Store owner/transaction boundary.

No runtime recovery, leases, or database file of its own. Legacy single-agent tables
and schema version are untouched; these extension tables are created only on opt-in.
"""
import json

from memory.repositories.tasks import encode


class WorkerPlanRepository:
    def __init__(self, store):
        self.store = store
        with store.transaction():
            store._conn.execute('CREATE TABLE IF NOT EXISTS worker_plan_heads ('
                                'task_id TEXT PRIMARY KEY REFERENCES tasks(task_id), '
                                'revision INTEGER NOT NULL, document TEXT NOT NULL)')
            store._conn.execute('CREATE TABLE IF NOT EXISTS worker_plan_history ('
                                'task_id TEXT NOT NULL REFERENCES tasks(task_id), '
                                'revision INTEGER NOT NULL, document TEXT NOT NULL, '
                                'PRIMARY KEY(task_id,revision))')

    def read(self, task_id):
        with self.store._lock:
            row = self.store._conn.execute('SELECT document FROM worker_plan_heads WHERE task_id=?',
                                           (task_id,)).fetchone()
            if row is None:
                raise ValueError('dag_plan_missing')
            value = json.loads(row[0])
            if value['schema_version'] != 1:
                raise ValueError('dag_schema_unsupported')
            return value

    def write(self, task_id, document, expected):
        if not self.store._conn.in_transaction:
            raise RuntimeError('dag_transaction_required')
        revision = document['plan']['revision']
        if expected == 0:
            cursor = self.store._conn.execute('INSERT OR IGNORE INTO worker_plan_heads VALUES(?,?,?)',
                                              (task_id, revision, encode(document)))
        else:
            cursor = self.store._conn.execute('UPDATE worker_plan_heads SET revision=?,document=? '
                                              'WHERE task_id=? AND revision=?',
                                              (revision, encode(document), task_id, expected))
        if cursor.rowcount != 1:
            raise ValueError('dag_cas_conflict')

    def archive(self, task_id, document):
        self.store._conn.execute('INSERT INTO worker_plan_history VALUES(?,?,?)',
                                 (task_id, document['plan']['revision'], encode(document)))

    def history(self, task_id):
        with self.store._lock:
            return tuple(json.loads(row[0]) for row in self.store._conn.execute(
                'SELECT document FROM worker_plan_history WHERE task_id=? ORDER BY revision', (task_id,)))
