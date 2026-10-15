"""B49 opt-in extension v1, under the existing Store transaction and owner.

No daemon, separate database, or changes to legacy rows. Installation is atomic;
rollback to v0 is allowed only while empty, otherwise retain the extension data.
"""
import json

from memory.repositories.tasks import encode

TABLES = ('team_inputs', 'team_messages', 'team_assignments', 'team_members', 'teams')


class TeamState:
    def __init__(self, store):
        self.store = store
        with store.transaction():
            db = store._conn
            db.execute('CREATE TABLE IF NOT EXISTS team_schema (version INTEGER PRIMARY KEY)')
            versions = [r[0] for r in db.execute('SELECT version FROM team_schema')]
            if versions and versions != [1]:
                raise ValueError('team_schema_unsupported')
            db.execute('CREATE TABLE IF NOT EXISTS teams ('
                       'id TEXT PRIMARY KEY, document TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS team_members ('
                       'id TEXT PRIMARY KEY, document TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS team_assignments ('
                       'id TEXT PRIMARY KEY, document TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS team_messages ('
                       'id TEXT PRIMARY KEY, document TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS team_inputs ('
                       'id TEXT PRIMARY KEY, document TEXT NOT NULL)')
            db.execute('INSERT OR IGNORE INTO team_schema VALUES(1)')

    def get(self, table, key):
        self._table(table)
        with self.store._lock:
            row = self.store._conn.execute(f'SELECT document FROM {table} WHERE id=?', (key,)).fetchone()
            if row is None:
                raise ValueError('team_record_missing')
            return json.loads(row[0])

    def all(self, table):
        self._table(table)
        with self.store._lock:
            return [json.loads(r[0]) for r in self.store._conn.execute(
                f'SELECT document FROM {table} ORDER BY rowid')]

    def put(self, table, key, value, *, create=False):
        self._table(table)
        if not self.store._conn.in_transaction:
            raise RuntimeError('team_transaction_required')
        if create:
            self.store._conn.execute(f'INSERT INTO {table} VALUES(?,?)', (key, encode(value)))
        else:
            cursor = self.store._conn.execute(f'UPDATE {table} SET document=? WHERE id=?',
                                              (encode(value), key))
            if cursor.rowcount != 1:
                raise ValueError('team_record_missing')

    def rollback_empty(self):
        with self.store.transaction():
            if any(self.all(t) for t in TABLES):
                raise ValueError('team_rollback_would_lose_data')
            for table in (*TABLES, 'team_schema'):
                self.store._conn.execute(f'DROP TABLE {table}')

    @staticmethod
    def _table(table):
        if table not in TABLES:
            raise ValueError('team_table_invalid')
