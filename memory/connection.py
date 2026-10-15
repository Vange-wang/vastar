"""连接生命周期：先锁、只读检查、再 WAL；不静默升级已有数据库。"""
import json
import sqlite3

from memory.migrations.schema import _SCHEMA, SCHEMA_VERSION
from memory.migrations.steps import upgrade
from memory.migrations.validation import inspect_database


def open_connection(path, now, *, require_existing):
    journal = path.with_name(path.name + ".migration.json")
    if journal.exists():
        phase = json.loads(journal.read_text(encoding="utf-8")).get("phase")
        if phase != "complete":
            raise ValueError(f"迁移尚未收尾（{phase}）；请显式重入 --migrate-storage 并执行后再启动")
    existing = path.exists()
    if existing:
        version = inspect_database(path)
        if version < SCHEMA_VERSION:
            raise ValueError(f"数据库 schema {version} 需要显式迁移；先 --migrate-storage \"{path}\" 预览")
    target = path.as_uri() + "?mode=rw" if require_existing else str(path)
    db = sqlite3.connect(target, uri=require_existing, check_same_thread=False)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        if not existing:
            db.executescript(_SCHEMA)
            with db:
                db.execute("INSERT INTO schema_version VALUES(1, ?)", (now(),))
            upgrade(db, 1, now)
        return db
    except BaseException:
        db.close()
        raise
