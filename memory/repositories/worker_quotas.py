"""Opt-in task money/time reservations. Caller holds the Store transaction."""
import json
from decimal import ROUND_CEILING, Decimal


def micros(yuan):
    value = Decimal(str(yuan))
    if not value.is_finite() or value < 0:
        raise ValueError('worker_cost_invalid')
    return int((value * 1000000).to_integral_value(rounding=ROUND_CEILING))


class WorkerQuotaRepository:
    def __init__(self, store, task_id, config, limit):
        self.db, self.task_id = store._conn, task_id
        self.base_cost = micros(min(config.max_cost_yuan, limit.max_cost_yuan or config.max_cost_yuan))
        self.db.execute('CREATE TABLE IF NOT EXISTS worker_quota_accounts ('
                        'account_id TEXT PRIMARY KEY, parent_id TEXT, owner TEXT, '
                        'cost INTEGER, seconds REAL, spent_cost INTEGER DEFAULT 0, '
                        'spent_seconds REAL DEFAULT 0, calls INTEGER, tokens INTEGER, '
                        'spent_calls INTEGER DEFAULT 0, spent_tokens INTEGER DEFAULT 0)')
        self.db.execute('CREATE TABLE IF NOT EXISTS worker_quota_reservations ('
                        'lease_id TEXT PRIMARY KEY, account_id TEXT, cost INTEGER, seconds REAL, '
                        'used_cost INTEGER DEFAULT 0, used_seconds REAL DEFAULT 0, '
                        'active_since REAL, pending_cost INTEGER, settled INTEGER DEFAULT 0, '
                        'detached INTEGER DEFAULT 0, result TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS worker_quota_activity ('
                        'lease_id TEXT, activity_id TEXT, cost INTEGER, PRIMARY KEY(lease_id,activity_id))')
        self.db.execute('CREATE TABLE IF NOT EXISTS worker_quota_policy (task_id TEXT PRIMARY KEY, policy TEXT)')
        policy = json.dumps([self.base_cost, min(config.max_active_seconds,
                             limit.max_active_seconds or config.max_active_seconds), config.budget_policy_version])
        self.db.execute('INSERT OR IGNORE INTO worker_quota_policy VALUES(?,?)', (task_id, policy))
        if self.db.execute('SELECT policy FROM worker_quota_policy WHERE task_id=?', (task_id,)).fetchone()[0] != policy:
            raise ValueError('worker_parent_budget_changed')
        self.db.execute('INSERT OR IGNORE INTO worker_quota_accounts '
                        '(account_id,cost,seconds,calls,tokens) VALUES(?,?,?,?,?)',
                        (task_id, micros(min(config.max_cost_yuan, limit.max_cost_yuan
                         or config.max_cost_yuan)), min(config.max_active_seconds,
                         limit.max_active_seconds or config.max_active_seconds),
                         limit.max_calls, limit.max_estimated_tokens))

    def account(self, account_id=None):
        return dict(self.db.execute('SELECT * FROM worker_quota_accounts WHERE account_id=?',
                                    (account_id or self.task_id,)).fetchone())

    def row(self, lease_id):
        return dict(self.db.execute('SELECT * FROM worker_quota_reservations WHERE lease_id=?',
                                    (lease_id,)).fetchone())

    def balance(self, account_id=None):
        account = self.account(account_id)
        held = self.db.execute('SELECT COALESCE(SUM(cost),0),COALESCE(SUM(seconds),0) '
                               'FROM worker_quota_reservations WHERE account_id=? AND settled=0',
                               (account['account_id'],)).fetchone()
        return dict(cost_yuan=(account['cost'] - account['spent_cost'] - held[0]) / 1000000,
                    active_seconds=account['seconds'] - account['spent_seconds'] - held[1])

    def reserve(self, token, budget, limit):
        # Default slices follow the existing token reservation; explicit slices may be smaller.
        cost = micros(budget.max_cost_yuan) if budget.max_cost_yuan is not None else (
            self.base_cost * budget.max_estimated_tokens // limit.max_estimated_tokens)
        seconds = budget.max_active_seconds or budget.deadline_seconds
        balance = self.balance()
        for dimension, requested in (('cost_yuan', cost / 1000000), ('active_seconds', seconds)):
            if requested > balance[dimension]:
                raise ValueError(f'worker_parent_budget_exhausted:{dimension}')
        self.db.execute('INSERT INTO worker_quota_reservations '
                        '(lease_id,account_id,cost,seconds) VALUES(?,?,?,?)',
                        (token.lease_id, self.task_id, cost, seconds))

    def check(self, lease_id, now):
        row = self.row(lease_id)
        elapsed = 0 if row['active_since'] is None else now - row['active_since']
        if elapsed < 0:
            raise ValueError('worker_active_clock_regressed')
        if row['settled'] or row['used_seconds'] + elapsed >= row['seconds']:
            raise ValueError('worker_parent_budget_exhausted:active_seconds')
        if row['used_cost'] > row['cost']:
            raise ValueError('worker_parent_budget_exhausted:cost_yuan')
        return row

    def begin(self, lease_id, now, cost=0, *, activity_id='manual'):
        row = self.check(lease_id, now)
        count, held = self.db.execute('SELECT COUNT(*),COALESCE(SUM(cost),0) FROM worker_quota_activity '
                                      'WHERE lease_id=?', (lease_id,)).fetchone()
        if count and row['active_since'] is None:
            raise ValueError('worker_activity_cancelled')
        if row['used_cost'] + held + cost > row['cost']:
            raise ValueError('worker_parent_budget_exhausted:cost_yuan')
        self.db.execute('INSERT INTO worker_quota_activity VALUES(?,?,?)', (lease_id, activity_id, cost))
        self.db.execute('UPDATE worker_quota_reservations SET active_since=COALESCE(active_since,?),pending_cost=? '
                        'WHERE lease_id=?', (now, held + cost, lease_id))

    def end(self, lease_id, now, cost=None, *, activity_id='manual'):
        row = self.row(lease_id)
        deleted = self.db.execute('DELETE FROM worker_quota_activity WHERE lease_id=? AND activity_id=?',
                                  (lease_id, activity_id)).rowcount
        if not deleted:
            return
        count, held = self.db.execute('SELECT COUNT(*),COALESCE(SUM(cost),0) FROM worker_quota_activity '
                                      'WHERE lease_id=?', (lease_id,)).fetchone()
        elapsed = 0 if count or row['active_since'] is None else now - row['active_since']
        elapsed = row['seconds'] if elapsed < 0 else elapsed
        # An unknown model charge consumes the entire remaining worker reservation.
        used = max(0, row['cost'] - row['used_cost']) if cost is None else cost
        self.db.execute('UPDATE worker_quota_reservations SET used_cost=used_cost+?, '
                        'used_seconds=used_seconds+?,active_since=?,pending_cost=? WHERE lease_id=?',
                        (used, elapsed, row['active_since'] if count else None, held if count else None, lease_id))

    def pause(self, lease_id, now):
        row = self.row(lease_id)
        if row['active_since'] is not None:
            elapsed = now - row['active_since']
            elapsed = row['seconds'] if elapsed < 0 else elapsed
            self.db.execute('UPDATE worker_quota_reservations SET used_seconds=used_seconds+?, '
                            'active_since=NULL WHERE lease_id=?', (elapsed, lease_id))

    def settle(self, lease_id, now, *, unstarted=False, unknown=False):
        row = self.row(lease_id)
        if row['settled']:
            return
        activities = self.db.execute('SELECT activity_id FROM worker_quota_activity WHERE lease_id=?',
                                      (lease_id,)).fetchall()
        for item in activities:
            self.end(lease_id, now, 0 if unstarted else None, activity_id=item[0])
        row = self.row(lease_id)
        cost = 0 if unstarted else max(row['cost'], row['used_cost']) if unknown else row['used_cost']
        seconds = 0 if unstarted else row['used_seconds']
        self.db.execute('UPDATE worker_quota_accounts SET spent_cost=spent_cost+?, '
                        'spent_seconds=spent_seconds+? WHERE account_id=?',
                        (cost, seconds, row['account_id']))
        self.db.execute('UPDATE worker_quota_reservations SET settled=1 WHERE lease_id=?', (lease_id,))

    def transferred(self):
        return tuple(self.db.execute('SELECT COALESCE(SUM(calls),0),COALESCE(SUM(tokens),0) '
                                     'FROM worker_quota_accounts WHERE parent_id=?',
                                     (self.task_id,)).fetchone())

    def transfer(self, lease, owner):
        row = self.row(lease['lease_id'])
        if row['detached'] or row['settled']:
            raise ValueError('worker_detach_already_transferred_or_settled')
        account_id = 'detached-' + lease['lease_id']
        self.db.execute('INSERT INTO worker_quota_accounts '
                        '(account_id,parent_id,owner,cost,seconds,calls,tokens) VALUES(?,?,?,?,?,?,?)',
                        (account_id, self.task_id, owner, row['cost'], row['seconds'],
                         lease['calls'], lease['tokens']))
        self.db.execute('UPDATE worker_quota_accounts SET cost=cost-?,seconds=seconds-?, '
                        'calls=calls-?,tokens=tokens-? WHERE account_id=?',
                        (row['cost'], row['seconds'], lease['calls'], lease['tokens'], self.task_id))
        self.db.execute('UPDATE worker_quota_reservations SET detached=1,account_id=? '
                        'WHERE lease_id=?', (account_id, lease['lease_id']))
        return account_id
