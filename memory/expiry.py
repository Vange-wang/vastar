"""失效事实保留审计，派生上下文不能继续冒充当前事实；重验须新来源修订。"""
import json

from memory import policy
from memory.expiry_sources import expiry_impact, quarantine_sources
from memory.forget_buffers import clear_caches
from memory.forget_cleanup import clean_snapshots

PREFIX = 'expiry-active-v2:'


def expire_contexts(store):
    with store.transaction():
        rows = store._conn.execute("SELECT record_json FROM memory_items WHERE status='active'").fetchall()
        for row in rows:
            item = json.loads(row[0])
            end = item['validity']['to']
            # 未到 from 不是已过期；不能使未来有效的来源永久失效。
            if not ((end and store._now() >= end) or (not end and policy.dynamic(item))):
                continue
            marker = f"{PREFIX}{item['id']}:{item['revision']}"
            if store._conn.execute('SELECT 1 FROM memory_proposals WHERE id=?', (marker,)).fetchone():
                continue
            entry = {'action_id': marker, 'impact': expiry_impact(store, item)}
            quarantine_sources(store._conn, entry['impact'])
            clean_snapshots(store._conn, entry, False)
            clear_caches(store, entry)
            store.memory_proposal(marker, None, entry, {'status': 'needs_revalidation', 'item_id': item['id'],
                                  'reason': 'expired_or_missing_dynamic_expiry'})
        # 旧 expiry 标记不会跳过 v2；新修订也不能使旧来源/迟到写入重新进入 active。
        entries = store._conn.execute("SELECT record_json FROM memory_proposals WHERE id LIKE ?",
                                      (PREFIX + '%',)).fetchall()
        for row in entries:
            entry = json.loads(row[0])
            if entry.get('impact') and quarantine_sources(store._conn, entry['impact']):
                clean_snapshots(store._conn, entry, False)
                clear_caches(store, entry)
