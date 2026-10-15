"""B18：操作账本和单实例写入代次。"""


def migrate_operations(db):
    db.execute("CREATE TABLE operation_epoch (id INTEGER PRIMARY KEY CHECK(id=1), generation INTEGER NOT NULL)")
    db.execute("INSERT INTO operation_epoch VALUES(1,0)")
    db.execute("""CREATE TABLE tool_operations (
        operation_id TEXT PRIMARY KEY, task_id TEXT, step_id TEXT, run_id TEXT NOT NULL,
        tool TEXT NOT NULL, tool_version TEXT NOT NULL, classification TEXT NOT NULL,
        args_hash TEXT NOT NULL, resource_version TEXT NOT NULL, approval_ref TEXT NOT NULL,
        state TEXT NOT NULL, receipt_ref TEXT, fencing_generation INTEGER NOT NULL,
        owner_id TEXT NOT NULL, facts TEXT NOT NULL, receipt TEXT, reconcile_result TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
