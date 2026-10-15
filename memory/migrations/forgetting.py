"""B23 schema 10：内容外的撤回元数据、会话上下文代次和失效产物。"""


def migrate_forgetting(db):
    for sql in (
        "CREATE TABLE memory_guard(id INTEGER PRIMARY KEY CHECK(id=1), journal_id TEXT)",
        "INSERT INTO memory_guard VALUES(1,NULL)",
        "CREATE TABLE memory_actions(action_id TEXT PRIMARY KEY, record_json TEXT NOT NULL)",
        "CREATE TABLE memory_tombstones(token TEXT PRIMARY KEY, action_id TEXT NOT NULL)",
        "CREATE TABLE memory_artifact_blocks(artifact_id TEXT, version INTEGER, action_id TEXT, "
        "PRIMARY KEY(artifact_id,version))",
        "ALTER TABLE sessions ADD COLUMN context_generation INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE sessions ADD COLUMN context_rebuild INTEGER NOT NULL DEFAULT 0",
    ):
        db.execute(sql)
