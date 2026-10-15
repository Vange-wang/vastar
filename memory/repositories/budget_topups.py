"""Immutable initial amounts and single-use, account-bound authorization receipts.

All operations run inside the existing Store transaction. Decimal credits preserve
half calls/tokens and sub-micro-yuan remainders; only whole units are spendable.
"""
import json
from decimal import Decimal
from uuid import uuid4

KEYS = ('calls', 'tokens', 'cost_yuan', 'active_seconds')


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def spendable(value):
    return dict(calls=int(Decimal(value['calls'])), tokens=int(Decimal(value['tokens'])),
                cost_yuan=int(Decimal(value['cost_yuan']) * 1000000) / 1000000,
                active_seconds=float(value['active_seconds']))


class TopupRepository:
    def __init__(self, store):
        self.store, self.db = store, store._conn
        self.db.execute('CREATE TABLE IF NOT EXISTS budget_topup_accounts ('
                        'account_id TEXT PRIMARY KEY, task_id TEXT, mode TEXT, initial TEXT, '
                        'total TEXT, ratio TEXT, ordinal INTEGER, challenge TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS budget_topup_receipts ('
                        'message_id TEXT PRIMARY KEY, account_id TEXT, ordinal INTEGER, body TEXT, '
                        'UNIQUE(account_id,ordinal))')

    def ensure(self, account_id, task_id, mode, initial, ratio):
        initial = {k: str(Decimal(str(initial[k]))) for k in KEYS}
        if any(not Decimal(v).is_finite() or Decimal(v) <= 0 for v in initial.values()):
            raise ValueError('budget_initial_invalid')
        self.db.execute('INSERT OR IGNORE INTO budget_topup_accounts VALUES(?,?,?,?,?,?,0,?)',
                        (account_id, task_id, mode, encoded(initial), encoded(initial), str(ratio), uuid4().hex))
        row = self.account(account_id)
        if row['initial'] != initial or Decimal(row['ratio']) != Decimal(str(ratio)) or row['mode'] != mode:
            raise ValueError('budget_initial_or_ratio_changed')
        return row

    def account(self, account_id):
        row = dict(self.db.execute('SELECT * FROM budget_topup_accounts WHERE account_id=?',
                                   (account_id,)).fetchone())
        for key in ('initial', 'total'):
            row[key] = json.loads(row[key])
        return row

    def increase(self, account_id, challenge, message_id, source, before, apply, balance):
        row = self.account(account_id)
        if self.db.execute('SELECT 1 FROM budget_topup_receipts WHERE message_id=?', (message_id,)).fetchone():
            raise ValueError('budget_authorization_already_used')
        if challenge != row['challenge']:
            raise ValueError('budget_authorization_stale')
        added = {k: str(Decimal(row['initial'][k]) * Decimal(row['ratio'])) for k in KEYS}
        total = {k: str(Decimal(row['total'][k]) + Decimal(added[k])) for k in KEYS}
        old, new = spendable(row['total']), spendable(total)
        delta = {k: new[k] - old[k] for k in KEYS}
        apply(delta)
        receipt = dict(account_id=account_id, task_id=row['task_id'], mode=row['mode'],
                       ordinal=row['ordinal'] + 1, initial=row['initial'], added=added,
                       spendable_added=delta, before=before, after=balance(), source=source,
                       actor='user', created_at=self.store._now())
        self.db.execute('INSERT INTO budget_topup_receipts VALUES(?,?,?,?)',
                        (message_id, account_id, receipt['ordinal'], encoded(receipt)))
        self.db.execute('UPDATE budget_topup_accounts SET total=?,ordinal=ordinal+1,challenge=? WHERE account_id=?',
                        (encoded(total), uuid4().hex, account_id))
        return receipt

    def history(self, account_id):
        return [json.loads(r[0]) for r in self.db.execute(
            'SELECT body FROM budget_topup_receipts WHERE account_id=? ORDER BY ordinal', (account_id,))]


def exists(store, account_id):
    db = store._conn
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='budget_topup_accounts'").fetchone():
        return False
    return db.execute('SELECT 1 FROM budget_topup_accounts WHERE account_id=?', (account_id,)).fetchone() is not None
