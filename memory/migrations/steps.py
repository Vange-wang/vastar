"""逐级迁移；DDL 逐条 execute，禁止 executescript 隐式提交破坏事务。"""
from memory.migrations.checkpoints import migrate_checkpoints
from memory.migrations.facts import migrate_facts
from memory.migrations.forgetting import migrate_forgetting
from memory.migrations.history import migrate_history
from memory.migrations.operations import migrate_operations
from memory.migrations.schema import ARTIFACTS_DDL, SCHEMA_VERSION
from memory.migrations.shared import migrate_shared
from memory.migrations.task_validation import migrate_task_validation
from memory.migrations.tasks import migrate_tasks


def upgrade_one(db, version):
    if version == 1:
        db.execute("ALTER TABLE runs ADD COLUMN owner_id TEXT")
        db.execute("ALTER TABLE runs ADD COLUMN owner_generation TEXT")
        db.execute("""CREATE TABLE runtime_owners (
            owner_id TEXT PRIMARY KEY, generation TEXT NOT NULL, pid INTEGER NOT NULL,
            started_at REAL NOT NULL, released_at TEXT
        )""")
        return
    if version == 2:
        db.execute(ARTIFACTS_DDL)   # B16：artifacts 元数据表（内容在受管目录，DB 只保元数据）
        return
    if version == 3:
        migrate_tasks(db)
        return
    if version == 4:
        migrate_operations(db)
        return
    if version == 5:
        migrate_checkpoints(db)
        return
    if version == 6:
        migrate_task_validation(db)
        return
    if version == 7:
        migrate_history(db)
        return
    if version == 8:
        migrate_facts(db)
        return
    if version == 9:
        migrate_forgetting(db)
        return
    if version == 10:
        migrate_shared(db)
        return
    raise ValueError(f"缺少 schema {version} → {version + 1} 的迁移步骤")


def upgrade(db, version, now, fault=lambda point: None):
    for current in range(version, SCHEMA_VERSION):
        db.execute("BEGIN IMMEDIATE")
        try:
            upgrade_one(db, current)
            fault("during_step")
            db.execute("INSERT INTO schema_version VALUES(?, ?)", (current + 1, now()))
            db.commit()
        except BaseException:
            db.rollback()
            raise
        fault("after_step")
