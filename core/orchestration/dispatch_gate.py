"""Vastar runtime component."""


def ensure(db):
    db.execute('CREATE TABLE IF NOT EXISTS worker_dispatch_state '
               '(task_id TEXT PRIMARY KEY, closed INTEGER NOT NULL, generation INTEGER NOT NULL)')


def close(service):
    with service.transaction():
        service.store._conn.execute('INSERT INTO worker_dispatch_state VALUES(?,1,1) '
                                    'ON CONFLICT(task_id) DO UPDATE SET closed=1,generation=generation+1',
                                    (service.plans.task_id,))


def check(service, lease_id=None):
    row = service.store._conn.execute('SELECT closed FROM worker_dispatch_state WHERE task_id=?',
                                      (service.plans.task_id,)).fetchone()
    if row and row[0] and (lease_id is None or not service.quotas.row(lease_id)['detached']):
        raise ValueError('worker_dispatch_closed')
