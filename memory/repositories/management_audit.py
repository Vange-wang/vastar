"""B50 opt-in 管理动作审计表：谁、何时、对哪个对象、什么动作、结果如何。

沿用 opt-in 扩展表模式（同 team_state/worker_plans）：构造时建表，不动编号迁移，
不加 schema 版本（b21/b22/b23 迁移断言无需连带）。被拒的动作同样留痕（result='rejected'）。
"""
import json
import uuid

TABLE = 'management_audit'


class ManagementAuditRepository:
    def __init__(self, store):
        self.store = store
        with store.transaction():
            store._conn.execute(
                'CREATE TABLE IF NOT EXISTS management_audit ('
                'audit_id TEXT PRIMARY KEY, ts REAL NOT NULL, actor TEXT NOT NULL, '
                'action TEXT NOT NULL, task_id TEXT NOT NULL, step_id TEXT, target TEXT, '
                'result TEXT NOT NULL, detail TEXT NOT NULL)')

    def record(self, *, action, task_id, actor, result, ts, step_id=None, target=None, detail=None):
        """调用者必须持有 Store 事务；审计与动作同事务落库，要么都成要么都不成。"""
        if not self.store._conn.in_transaction:
            raise RuntimeError('management_audit_transaction_required')
        audit_id = 'audit-' + uuid.uuid4().hex
        payload = json.dumps(detail or {}, ensure_ascii=False, sort_keys=True)
        self.store._conn.execute(
            'INSERT INTO management_audit VALUES(?,?,?,?,?,?,?,?,?)',
            (audit_id, ts, actor, action, task_id, step_id, target, result, payload))
        return audit_id

    def list(self, task_id=None, *, limit=50):
        """按时间正序返回最近 limit 条（谁、何时、对象、动作、结果）。"""
        with self.store._lock:
            if task_id:
                rows = self.store._conn.execute(
                    'SELECT * FROM management_audit WHERE task_id=? ORDER BY rowid DESC LIMIT ?',
                    (task_id, limit)).fetchall()
            else:
                rows = self.store._conn.execute(
                    'SELECT * FROM management_audit ORDER BY rowid DESC LIMIT ?', (limit,)).fetchall()
        return [dict(r) for r in reversed(rows)]


def has_table(store):
    return store._conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='management_audit'").fetchone() is not None
