"""C04：旧会话仅增加可空关联，不从聊天或摘要生成任务。"""

TASK_DDL = (
    """CREATE TABLE tasks (
        task_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, goal TEXT NOT NULL,
        source_ref TEXT NOT NULL, constraints TEXT NOT NULL, acceptance TEXT NOT NULL,
        status TEXT NOT NULL, current_plan_revision INTEGER NOT NULL,
        budget TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        revision INTEGER NOT NULL, clarifications TEXT NOT NULL DEFAULT '[]')""",
    """CREATE TABLE task_steps (
        step_id TEXT NOT NULL, task_id TEXT NOT NULL REFERENCES tasks(task_id),
        goal TEXT NOT NULL, depends_on TEXT NOT NULL, status TEXT NOT NULL,
        evidence_refs TEXT NOT NULL, remaining_work TEXT NOT NULL,
        owner_id TEXT, generation TEXT, revision INTEGER NOT NULL,
        PRIMARY KEY(task_id, step_id))""",
    """CREATE TABLE plan_revisions (
        task_id TEXT NOT NULL REFERENCES tasks(task_id), revision INTEGER NOT NULL,
        reason TEXT NOT NULL, proposal TEXT NOT NULL, actor TEXT NOT NULL,
        created_at TEXT NOT NULL, PRIMARY KEY(task_id, revision))""",
    """CREATE TABLE session_tasks (
        session_id TEXT PRIMARY KEY REFERENCES sessions(session_id),
        task_id TEXT NOT NULL REFERENCES tasks(task_id))""",
    "ALTER TABLE runs ADD COLUMN task_id TEXT REFERENCES tasks(task_id)",
    "ALTER TABLE runs ADD COLUMN step_id TEXT",
)


def migrate_tasks(db):
    for statement in TASK_DDL:
        db.execute(statement)
