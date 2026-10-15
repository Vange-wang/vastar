"""B20：验收记录、独立于授权的提问、跨 Run 任务预算。"""


def migrate_task_validation(db):
    db.execute("ALTER TABLE task_steps ADD COLUMN acceptance_ids TEXT NOT NULL DEFAULT '[]'")
    db.execute("ALTER TABLE task_steps ADD COLUMN independent INTEGER NOT NULL DEFAULT 0")
    db.execute("""CREATE TABLE task_validations (
        validation_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
        step_id TEXT NOT NULL, plan_revision INTEGER NOT NULL, result TEXT NOT NULL,
        reviewer TEXT NOT NULL, created_at TEXT NOT NULL)""")
    db.execute("""CREATE TABLE task_questions (
        question_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
        plan_revision INTEGER NOT NULL, question TEXT NOT NULL, options TEXT NOT NULL,
        required INTEGER NOT NULL, expires_at REAL NOT NULL, state TEXT NOT NULL,
        answers TEXT NOT NULL, created_at TEXT NOT NULL)""")
    db.execute("""CREATE TABLE task_runtime (
        task_id TEXT PRIMARY KEY REFERENCES tasks(task_id), used_calls INTEGER NOT NULL DEFAULT 0,
        reserved_calls INTEGER NOT NULL DEFAULT 0, active_run TEXT,
        recovery_error TEXT, rework TEXT NOT NULL DEFAULT '{}', receipts TEXT NOT NULL DEFAULT '[]')""")
