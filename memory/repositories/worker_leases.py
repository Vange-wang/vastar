"""B45 opt-in execution ledger. All mutations use the caller's Store transaction."""
import json
from dataclasses import asdict


class WorkerLeaseRepository:
    def __init__(self, store, task_id, limit):
        self.store, self.task_id = store, task_id
        with store.transaction():
            db = store._conn
            db.execute('CREATE TABLE IF NOT EXISTS worker_lease_accounts ('
                       'task_id TEXT PRIMARY KEY REFERENCES tasks(task_id), limits TEXT NOT NULL, '
                       'spent_calls INTEGER NOT NULL, spent_tokens INTEGER NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS worker_leases ('
                       'lease_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, step_id TEXT NOT NULL, '
                       'revision INTEGER NOT NULL, generation INTEGER NOT NULL, owner TEXT NOT NULL, '
                       'expires REAL NOT NULL, state TEXT NOT NULL, started INTEGER NOT NULL, '
                       'settled INTEGER NOT NULL, calls INTEGER NOT NULL, tokens INTEGER NOT NULL, '
                       'UNIQUE(task_id,step_id,generation))')
            db.execute('CREATE TABLE IF NOT EXISTS worker_resource_locks ('
                       'resource TEXT PRIMARY KEY, lease_id TEXT NOT NULL REFERENCES worker_leases(lease_id))')
            encoded = json.dumps(asdict(limit), sort_keys=True)
            db.execute('INSERT OR IGNORE INTO worker_lease_accounts VALUES(?,?,0,0)', (task_id, encoded))
            stored = json.loads(self.account()['limits'])
            expected = asdict(limit)
            if any(stored.get(k) != v for k, v in expected.items()):
                raise ValueError('worker_parent_budget_changed')

    def account(self):
        return self.store._conn.execute('SELECT * FROM worker_lease_accounts WHERE task_id=?',
                                        (self.task_id,)).fetchone()

    def available(self):
        row = self.account()
        limit = self.store._conn.execute('SELECT calls,tokens FROM worker_quota_accounts WHERE account_id=?',
                                         (self.task_id,)).fetchone()
        held = self.store._conn.execute('SELECT COALESCE(SUM(calls),0),COALESCE(SUM(tokens),0) '
                                        'FROM worker_leases WHERE task_id=? AND settled=0 '
                                        'AND lease_id NOT IN (SELECT lease_id FROM '
                                        'worker_quota_reservations WHERE detached=1)',
                                        (self.task_id,)).fetchone()
        # This account already excludes detached grants and includes authorized new credit.
        return (limit['calls'] - row['spent_calls'] - held[0],
                limit['tokens'] - row['spent_tokens'] - held[1])

    def get(self, lease_id):
        row = self.store._conn.execute('SELECT * FROM worker_leases WHERE task_id=? AND lease_id=?',
                                       (self.task_id, lease_id)).fetchone()
        if row is None:
            raise ValueError('worker_lease_missing')
        return dict(row)

    def expired(self, now):
        return [dict(r) for r in self.store._conn.execute(
            "SELECT * FROM worker_leases WHERE task_id=? AND state='active' AND expires<=?",
            (self.task_id, now))]

    def generation(self, step_id):
        return self.store._conn.execute('SELECT COALESCE(MAX(generation),0)+1 FROM worker_leases '
                                        'WHERE task_id=? AND step_id=?', (self.task_id, step_id)).fetchone()[0]

    def insert(self, token, budget, expires, resources):
        self.store._conn.execute('INSERT INTO worker_leases VALUES(?,?,?,?,?,?,?,\'active\',0,0,?,?)',
                                (token.lease_id, self.task_id, token.step_id, token.revision,
                                 token.generation, token.owner, expires, budget.max_calls,
                                 budget.max_estimated_tokens))
        for resource in resources:
            self.store._conn.execute('INSERT INTO worker_resource_locks VALUES(?,?)',
                                    (resource, token.lease_id))

    def locks_available(self, resources):
        return all(self.store._conn.execute('SELECT 1 FROM worker_resource_locks WHERE resource=?',
                                            (key,)).fetchone() is None for key in resources)

    def settle(self, lease_id, calls=None, estimated_tokens=None):
        row = self.get(lease_id)
        if row['settled']:
            raise ValueError('worker_reservation_already_settled')
        values = calls, estimated_tokens
        if any(v is not None and (type(v) is not int or v < 0) for v in values):
            raise ValueError('worker_usage_invalid')
        # A missing component charges the full reservation, never zero. Overshoot is debt.
        used = (row['calls'], row['tokens']) if None in values else values
        independent = self.store._conn.execute('SELECT account_id FROM worker_quota_reservations '
                                               'WHERE lease_id=? AND detached=1', (lease_id,)).fetchone()
        if independent:
            self.store._conn.execute('UPDATE worker_quota_accounts SET spent_calls=spent_calls+?, '
                                    'spent_tokens=spent_tokens+? WHERE account_id=?', (*used, independent[0]))
        else:
            self.store._conn.execute('UPDATE worker_lease_accounts SET spent_calls=spent_calls+?, '
                                    'spent_tokens=spent_tokens+? WHERE task_id=?', (*used, self.task_id))
        self.store._conn.execute('UPDATE worker_leases SET settled=1 WHERE lease_id=?', (lease_id,))

    def close(self, lease_id, state):
        self.store._conn.execute('UPDATE worker_leases SET state=? WHERE lease_id=?', (state, lease_id))
        self.store._conn.execute('DELETE FROM worker_resource_locks WHERE lease_id=?', (lease_id,))


def has_account(store, task_id):
    """No DDL on legacy/default-off paths."""
    if not store._conn.execute("SELECT 1 FROM sqlite_master WHERE name='worker_lease_accounts'").fetchone():
        return False
    return store._conn.execute('SELECT 1 FROM worker_lease_accounts WHERE task_id=?', (task_id,)).fetchone() is not None
