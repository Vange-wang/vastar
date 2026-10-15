"""数据库外的不可自动丢弃撤回日志；先落意图，再落事务，崩溃后幂等重放。"""
import hashlib
import json
import uuid

from core.ownership import atomic_json
from memory.repositories.facts import encode


def token(*parts):
    return hashlib.sha256(encode(parts).encode()).hexdigest()


def journal_path(store):
    return store._path.with_name(store._path.name + '.revocations.json')


def read_journal(store):
    path = journal_path(store)
    expected = store._conn.execute('SELECT journal_id FROM memory_guard WHERE id=1').fetchone()[0]
    if not path.exists():
        if expected:
            raise ValueError('撤回日志缺失；拒绝恢复/检索。恢复备份须携带最新 revocations.json，不能用旧副本覆盖。')
        value = {'journal_id': uuid.uuid4().hex, 'entries': []}
        atomic_json(path, value)
    else:
        value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value.get('entries'), list) or not value.get('journal_id'):
        raise ValueError('撤回日志损坏；拒绝猜补')
    if expected and value['journal_id'] != expected:
        raise ValueError('撤回日志不属于此数据库；拒绝恢复')
    if expected is None:
        with store.transaction():
            store._conn.execute('UPDATE memory_guard SET journal_id=? WHERE id=1', (value['journal_id'],))
    applied = {r[0] for r in store._conn.execute('SELECT action_id FROM memory_actions')}
    if not applied.issubset({e['action_id'] for e in value['entries']}):
        raise ValueError('撤回日志水位落后于数据库；拒绝启动')
    return value


def append_intent(store, entry):
    value = read_journal(store)
    old = next((e for e in value['entries'] if e['action_id'] == entry['action_id']), None)
    if old is not None and old != entry:
        raise ValueError('action_id_parameter_conflict')
    if old is None:
        value['entries'].append(entry)
        atomic_json(journal_path(store), value)


def replay(store):
    from memory.forget_cleanup import apply_entry
    for entry in read_journal(store)['entries']:
        with store.transaction():
            apply_entry(store, entry)
    store._privacy_pending = False


def ensure_replayed(store):
    if getattr(store, '_privacy_pending', False):
        replay(store)


def blocked(store, scope, key):
    return store._conn.execute('SELECT 1 FROM memory_tombstones WHERE token=?', (token(scope, key),)).fetchone() is not None
