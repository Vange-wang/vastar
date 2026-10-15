"""B22 schema 9：当前事实、候选、同一审计修订链；旧无来源记忆不自动晋升。"""


def migrate_facts(db):
    for sql in (
        "CREATE TABLE memory_items(id TEXT PRIMARY KEY, scope TEXT NOT NULL, kind TEXT NOT NULL, "
        "key TEXT NOT NULL, text TEXT NOT NULL, revision INTEGER NOT NULL, status TEXT NOT NULL, "
        "record_json TEXT NOT NULL, UNIQUE(scope,key))",
        "CREATE TABLE memory_proposals(id TEXT PRIMARY KEY, session_id TEXT, status TEXT NOT NULL, "
        "record_json TEXT NOT NULL, receipt_json TEXT NOT NULL, created_at TEXT NOT NULL)",
        "CREATE TABLE memory_index(item_id TEXT PRIMARY KEY, scope TEXT NOT NULL, kind TEXT NOT NULL, "
        "text TEXT NOT NULL, revision INTEGER NOT NULL)",
        "CREATE INDEX memory_scope_index ON memory_index(scope,kind)",
        "ALTER TABLE memory_revisions ADD COLUMN item_json TEXT",
        "ALTER TABLE memory_revisions ADD COLUMN proposal_id TEXT",
        "ALTER TABLE sessions ADD COLUMN memory_revision INTEGER",
        "ALTER TABLE sessions ADD COLUMN memory_snapshot_json TEXT",
        "ALTER TABLE sessions ADD COLUMN memory_read_enabled INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE sessions ADD COLUMN memory_grants_json TEXT NOT NULL DEFAULT '[]'",
    ):
        db.execute(sql)
    # 原表保留原文/删除/回滚链；没有可靠来源的旧记录仅候选，不注入上下文。
    db.execute("""INSERT INTO memory_proposals
        SELECT 'legacy-memory-'||revision,NULL,'needs_review',
        json_object('legacy_revision',revision,'source_refs',json('[]')),
        '{"status":"needs_review","reason":"legacy_source_unverified"}',created_at
        FROM memory_revisions WHERE item_json IS NULL""")
