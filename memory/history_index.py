"""可重建 trigram 索引；每次匹配只接受已授权候选 id。"""
import sqlite3


def create_index(db):
    try:
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS history_fts USING fts5(body, tokenize='trigram')")
        return True
    except sqlite3.DatabaseError:
        return False


def index_row(db, row):
    old = db.execute("SELECT revision FROM history_index_state WHERE id=?", (row['id'],)).fetchone()
    if old is None or old[0] != row['revision']:
        db.execute("DELETE FROM history_fts WHERE rowid=?", (row['id'],))
        db.execute("INSERT INTO history_fts(rowid,body) VALUES(?,?)", (row['id'], row['body']))
        db.execute("INSERT OR REPLACE INTO history_index_state VALUES(?,?)", (row['id'], row['revision']))


def match_rows(store, rows, query):
    """短词明确 fallback；坏索引不影响权威数据，不扩大候选范围。"""
    if len(query) < 3:
        return [r for r in rows if query.casefold() in r['body'].casefold()], 'bounded_short_substring'
    try:
        with store.transaction():
            hits = []
            for row in rows:
                index_row(store._conn, row)
                found = store._conn.execute(
                    'SELECT rowid FROM history_fts WHERE rowid=? AND history_fts MATCH ?',
                    (row['id'], '"' + query.replace('"', '""') + '"')).fetchone()
                # 读回权威文本再核对，索引中的孤儿/旧版本绝不返回。
                literal = query.casefold() in row['body'].casefold()
                if literal and not found:
                    raise sqlite3.DatabaseError('索引缺项/不一致，使用已授权候选回退')
                if found and literal:
                    hits.append(row)
            return hits, 'fts5_trigram_literal'
    except sqlite3.DatabaseError:
        return [r for r in rows if query.casefold() in r['body'].casefold()], 'bounded_index_fallback'


def rebuild(service):
    """按授权范围分页、每页短事务；禁扫描其他用户/项目的原文。"""
    count, cursor = 0, 0
    with service.store.transaction():
        db = service.store._conn
        db.execute('DROP TABLE IF EXISTS history_fts')
        db.execute('DELETE FROM history_index_state')
        if not create_index(db):
            return {'status': 'unavailable', 'reason': 'SQLite FTS5 trigram 不可用'}
    while True:
        page = service.list(after=cursor, limit=100)
        if page['status'] not in ('ok', 'empty'):
            return page
        with service.store.transaction():
            for row in page['items']:
                index_row(service.store._conn, row)
                count += 1
        if page['next_cursor'] is None:
            return {'status': 'ok', 'indexed': count}
        cursor = page['next_cursor']
