"""Shared proposals are private content surfaces, including pending/rejected rows."""

from memory.forget_journal import token
from memory.repositories.facts import encode


def clean_agent_rows(db, impact):
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ('agent_proposals', 'memory_conflicts'):
        if table not in names:
            continue
        for row in db.execute(f'SELECT * FROM {table}').fetchall():
            body = row['record_json']
            linked = token(row['scope'], row['key']) in impact['tokens']
            linked |= any(item in body for item in impact['items'])
            if not linked:
                continue
            if table == 'agent_proposals':
                db.execute("UPDATE agent_proposals SET key='',record_json='{}',receipt_json=?,"
                           "status='redacted' WHERE id=?",
                           (encode(dict(status='redacted', reason='privacy_deleted')), row['id']))
            else:
                db.execute('DELETE FROM memory_conflicts WHERE id=?', (row['id'],))


def invalidate_conflict(store, item):
    from memory.forget_cleanup import clean_snapshots
    from memory.forget_plan import plan
    impact = plan(store, item)
    entry = dict(impact=impact, action_id='conflict:' + item['id'])
    clean_snapshots(store._conn, entry, False)
