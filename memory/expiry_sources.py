"""到期来源退出 active 输入；保留正文与版本供审计，不执行隐私删除。"""
from memory.forget_plan import plan, source_id


def expiry_impact(store, item):
    impact = plan(store, item)
    original = {source_id(ref) for ref in item['source_refs']}
    end = item['validity']['to']
    # 到期后的用户新确认是新证据；不能因相同文字而一并永久封锁。
    if end:
        fresh = {r[0] for r in store._conn.execute(
            "SELECT message_id FROM messages WHERE role='user' AND created_at>=?", (end,))}
        impact['sources'] = sorted(set(impact['sources']) - (fresh - original))
    return impact


def quarantine_sources(db, impact):
    sources, runs = set(impact['sources']), set(impact['runs'])
    changed = False
    for row in db.execute('SELECT h.*,m.role FROM history_sources h LEFT JOIN messages m '
                          'ON m.message_id=h.source_id').fetchall():
        # 已受污染的运行迟到写入也退出 active；不封锁新运行或用户新证据。
        affected = row['source_id'] in sources or (row['run_id'] in runs and row['role'] != 'user')
        if not affected or row['status'] != 'active':
            continue
        db.execute("UPDATE history_sources SET status='obsolete',revision=revision+1 WHERE id=?", (row['id'],))
        db.execute('DELETE FROM history_index_state WHERE id=?', (row['id'],))
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='history_fts'").fetchone():
            db.execute('DELETE FROM history_fts WHERE rowid=?', (row['id'],))
        changed = True
    return changed
