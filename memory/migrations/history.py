"""B21 schema 8：原文/版本为权威，FTS 为可丢弃的派生索引。"""


def migrate_history(db):
    for sql in (
        "CREATE TABLE history_identity(id INTEGER PRIMARY KEY CHECK(id=1), user_id TEXT NOT NULL, project_id TEXT NOT NULL)",
        "CREATE TABLE history_scopes(session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, project_id TEXT NOT NULL)",
        """CREATE TABLE history_sources(
            id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT UNIQUE NOT NULL,
            session_id TEXT NOT NULL, run_id TEXT, kind TEXT NOT NULL,
            body TEXT NOT NULL, record_json TEXT NOT NULL DEFAULT '{}',
            revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL)""",
        """CREATE TABLE history_versions(source_id TEXT NOT NULL, revision INTEGER NOT NULL,
            body TEXT NOT NULL, record_json TEXT NOT NULL, status TEXT NOT NULL,
            PRIMARY KEY(source_id,revision))""",
        "CREATE INDEX history_scope_lookup ON history_scopes(user_id,project_id,session_id)",
        "CREATE INDEX history_source_session ON history_sources(session_id,id)",
        "CREATE INDEX history_source_run ON history_sources(run_id,id)",
        "CREATE INDEX history_source_time ON history_sources(created_at,id)",
        "CREATE TABLE history_model_calls(call_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, record_json TEXT NOT NULL)",
        "CREATE INDEX history_call_run ON history_model_calls(run_id)",
        "CREATE TABLE history_index_state(id INTEGER PRIMARY KEY, revision INTEGER NOT NULL)",
    ):
        db.execute(sql)
    db.execute("""INSERT INTO history_sources(source_id,session_id,run_id,kind,body,status,created_at)
        SELECT message_id,session_id,run_id,'message',content,
        CASE WHEN status IN ('complete','partial') THEN 'active' ELSE status END,created_at FROM messages""")
    db.execute("""CREATE TRIGGER history_message_insert AFTER INSERT ON messages BEGIN
        INSERT INTO history_sources(source_id,session_id,run_id,kind,body,status,created_at)
        VALUES(new.message_id,new.session_id,new.run_id,'message',new.content,
        CASE WHEN new.status IN ('complete','partial') THEN 'active' ELSE new.status END,new.created_at); END""")
    db.execute("""CREATE TRIGGER history_message_update AFTER UPDATE ON messages BEGIN
        UPDATE history_sources SET body=new.content,run_id=new.run_id,revision=revision+1,
        status=CASE WHEN new.status IN ('complete','partial') THEN 'active' ELSE new.status END
        WHERE source_id=old.message_id AND status NOT IN ('withdrawn','deleted'); END""")
    db.execute("""CREATE TRIGGER history_message_delete AFTER DELETE ON messages BEGIN
        DELETE FROM history_sources WHERE source_id=old.message_id; END""")
    db.execute("""CREATE TRIGGER history_version BEFORE UPDATE ON history_sources BEGIN
        INSERT OR IGNORE INTO history_versions VALUES(old.source_id,old.revision,
        old.body,old.record_json,old.status); END""")
    db.execute("""CREATE TRIGGER history_scope_insert AFTER INSERT ON sessions BEGIN
        INSERT INTO history_scopes SELECT new.session_id,user_id,project_id FROM history_identity; END""")
    # 无 FTS5 的 SQLite 仍能迁移；服务显式使用有界回退，不假称索引可用。
    from memory.history_index import create_index
    create_index(db)
    backfill_events(db)


def backfill_events(db):
    """旧事件只保留已知证据，不从缺失的 call_id/完整观察猜造配对。"""
    db.execute("""INSERT INTO history_sources(source_id,session_id,run_id,kind,body,record_json,created_at)
        SELECT event_id,session_id,run_id,'legacy_tool_event',payload,
        '{"pair_status":"legacy_event_only","note":"旧事件无完整配对；不猜补"}',timestamp_utc
        FROM events WHERE type IN ('tool.requested','tool.executed','tool.failed','tool.rejected','tool.cancelled')
        ORDER BY rowid""")
    db.execute("""INSERT OR REPLACE INTO history_model_calls(call_id,run_id,record_json)
        SELECT json_extract(payload,'$.call_id'),run_id,payload FROM events
        WHERE type IN ('model_call.intent','model_call.finished')
        AND json_valid(payload) AND json_extract(payload,'$.call_id') IS NOT NULL ORDER BY rowid""")
