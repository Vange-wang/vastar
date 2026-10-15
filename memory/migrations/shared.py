"""B48 schema 11：多 Agent 共享记忆的提案队列与持久冲突记录。"""


def migrate_shared(db):
    db.execute("CREATE TABLE agent_proposals(id TEXT PRIMARY KEY, agent TEXT NOT NULL, task_id TEXT NOT NULL, "
               "scope TEXT NOT NULL, key TEXT NOT NULL, status TEXT NOT NULL, record_json TEXT NOT NULL, "
               "receipt_json TEXT NOT NULL, created_at TEXT NOT NULL, expires_at REAL, decided_at TEXT)")
    db.execute("CREATE INDEX agent_proposals_status ON agent_proposals(status)")
    db.execute("CREATE TABLE memory_conflicts(id TEXT PRIMARY KEY, scope TEXT NOT NULL, key TEXT NOT NULL, "
               "record_json TEXT NOT NULL, created_at TEXT NOT NULL)")
