"""B19 安全边界快照与输入队列，不从摘要恢复事实。"""


def migrate_checkpoints(db):
    db.execute("""CREATE TABLE checkpoints (
        checkpoint_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
        body TEXT NOT NULL, hash TEXT NOT NULL, state TEXT NOT NULL,
        created_at TEXT NOT NULL)""")
    db.execute("""CREATE TABLE pending_inputs (
        input_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(session_id),
        run_id TEXT NOT NULL UNIQUE, body TEXT NOT NULL, state TEXT NOT NULL,
        created_at TEXT NOT NULL)""")
