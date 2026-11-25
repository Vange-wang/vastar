"""Recover existing outputs only; uncertain call/lease reservations remain charged/held."""
from contextlib import contextmanager, nullcontext


def recover_operation(recovery, operation_id, *, write_guard=nullcontext):
    store = recovery.store
    op = store.operation_get(operation_id)
    managed = store._conn.execute('SELECT active_run FROM task_runtime WHERE task_id=?',
                                  (op['task_id'],)).fetchone()
    if managed and managed[0] == op['run_id'] and op['state'] in {'executing', 'unknown'}:
        result = {'status': 'unknown', 'evidence': {
            'reason': '动作已发出但回单未确认；请显式 /task reconcile ' + operation_id}}
        with write_guard():
            store.operation_finish(operation_id, 'unknown', op['receipt'], op['facts'], reconciliation=result)
        return result
    return recovery.ledger.reconcile(operation_id, write_guard=write_guard)


def abandoned_run(store, run):
    return (run['terminal_state'] in {'INTERRUPTED', 'COMPLETED'}
            and (run['owner_id'] != store._owner.owner_id
                 or run['owner_generation'] != store._owner.generation))


class VerificationSuperseded(Exception):
    """Foreground activity invalidated the background inspection."""


class BackgroundVerification:
    """Fence recovery writes with the idle task state observed before expensive reads."""
    def __init__(self, store, task_id, checkpoint_limit):
        self.store, self.task_id, self.checkpoint_limit = store, task_id, checkpoint_limit
        with store._lock:
            self.observed = self._state()
            if self.observed is None:
                raise VerificationSuperseded()

    def _state(self):
        row = self.store._conn.execute(
            'SELECT t.revision, rt.active_run, rt.recovery_error, rt.used_calls, '
            'length(CAST(rt.receipts AS BLOB)), r.run_id, r.owner_id, r.owner_generation, r.terminal_state '
            'FROM tasks t LEFT JOIN task_runtime rt ON rt.task_id=t.task_id '
            'LEFT JOIN runs r ON r.run_id=rt.active_run WHERE t.task_id=? AND NOT EXISTS '
            '(SELECT 1 FROM checkpoints c WHERE c.rowid>? AND c.task_id=t.task_id)',
            (self.task_id, self.checkpoint_limit)).fetchone()
        if row is None or (row['active_run'] and (not row['run_id'] or not abandoned_run(self.store, row))):
            return None
        return tuple(row)

    @contextmanager
    def write(self):
        # Store transactions share this RLock. Hashing and other evidence reads
        # finish before entering; the guard covers the comparison and DB write.
        with self.store._lock:
            if self._state() != self.observed:
                raise VerificationSuperseded()
            yield
            self.observed = self._state()


def permit_retained_budget(store, task, checkpoint, active_run, delivery_run):
    run = store._conn.execute('SELECT * FROM runs WHERE run_id=? AND task_id=?',
                              (active_run, task['task_id'])).fetchone()
    if run and abandoned_run(store, run):
        if delivery_run == active_run:
            return
        if task['status'] == checkpoint['task']['status'] == 'COMPLETED':
            return  # Delivery checkpoint does not claim that unknown usage was settled.
    raise ValueError('Run 预算尚未结算，禁止重置；已有成果可 /task deliver 核验交付，未知预算仍保留')


def recover_delivery(coord, task, run):
    """Explicit delivery is not permission to rerun a model or an external operation."""
    if task['status'] in {'CANCELLED', 'BLOCKED', 'COMPLETED'} or not abandoned_run(coord._store, run):
        raise ValueError('本轮未正常结束或已取消；不验收、不产生交付引用')
    report = coord.recovery.recover(task['task_id'], delivery_run=run['run_id'])
    if report['status'] == 'needs_input':
        raise ValueError(report['reason'])
    return {'run_state': run['terminal_state'], 'recovered_outputs_only': True,
            'budget': '未知调用与子租约预算占用保留；未重置、未再次扣费'}
