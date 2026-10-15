"""只读 schema 验证；必须先于 WAL 设置或任何 DDL。"""
import sqlite3
from contextlib import closing
from pathlib import Path

from memory.migrations.schema import SCHEMA_VERSION

RESTORE_GUIDE = "回退代码不等于回退数据；先保全升级后的新数据，再离线恢复迁移前备份；禁止覆盖新库。"


def inspect_database(path, *, supported=SCHEMA_VERSION):
    path = Path(path).resolve(strict=True)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        versions = [r[0] for r in db.execute("SELECT version FROM schema_version ORDER BY version")]
        version = versions[-1] if versions else 0
        if version > supported:
            raise ValueError(f"不支持的数据库 schema {version}（程序最高 {supported}）。{RESTORE_GUIDE}")
        if not versions or versions != list(range(1, version + 1)):
            raise ValueError("数据库 schema 版本链不连续；拒绝修改原库")
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("数据库完整性检查失败；拒绝修改原库")
        return version
