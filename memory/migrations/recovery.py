"""迁移凭据与候选库校验；任何失败保留备份，绝不自动覆盖新数据。"""
import hashlib
import json
import sqlite3
import time
from contextlib import closing

from core.ownership import atomic_json
from memory.migrations.validation import inspect_database


def snapshot(path):
    """对逻辑内容取指纹；WAL 与主库共同读取，避免物理 hash 被 checkpoint 干扰。"""
    version = inspect_database(path)
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        try:
            if db.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("数据库外键检查失败")
            tables = [r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            counts = {}
            digest = hashlib.sha256()
            for table in tables:
                escaped = table.replace('"', '""')
                try:
                    rows = db.execute(f'SELECT * FROM "{escaped}" ORDER BY rowid').fetchall()
                except sqlite3.OperationalError as exc:
                    if 'no such column: rowid' not in str(exc):
                        raise
                    columns = len(db.execute(f'PRAGMA table_info("{escaped}")').fetchall())
                    order = ','.join(str(i + 1) for i in range(columns))
                    rows = db.execute(f'SELECT * FROM "{escaped}" ORDER BY {order}').fetchall()
                counts[table] = len(rows)
                digest.update(json.dumps([table, rows], ensure_ascii=False,
                                         default=lambda value: {'bytes_hex': value.hex()}).encode("utf-8"))
            return {"version": version, "counts": counts, "digest": digest.hexdigest()}
        finally:
            db.rollback()  # 短只读事务仅用于维护，UI/后台索引不调用。


def copy_database(source, destination, *, must_exist=False, progress=None):
    """SQLite backup 目标端是一个原子写事务，支持 WAL，失败回滚。"""
    target = destination.resolve().as_uri() + "?mode=rw" if must_exist else str(destination)
    deadline = time.monotonic() + 5

    def advance(status, remaining, total):
        if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) and time.monotonic() > deadline:
            raise TimeoutError("SQLite backup 等待写锁超时；原库未切换，请停止外部数据库写入者")
        if progress:
            progress(status, remaining, total)
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(target, uri=must_exist, timeout=5)) as dst:
            dst.execute("PRAGMA busy_timeout=5000")
            src.backup(dst, pages=64, progress=advance)


def recover_switch(journal_path, path):
    if not journal_path.exists():
        return None
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    if journal.get("phase") != "switching":
        return None
    facts = snapshot(path)
    if facts == journal["candidate_facts"]:
        journal["phase"] = "complete"
        journal["recovered_after_switch"] = True
        atomic_json(journal_path, journal)
        return journal
    if facts != journal["source_facts"]:
        raise ValueError("中断迁移后库内容与源/候选均不符；保留现场与备份，拒绝覆盖")
    return None  # 原子切换尚未提交，允许从完整旧库重新生成候选。
