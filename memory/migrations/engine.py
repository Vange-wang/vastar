"""显式、可预览的离线 schema 迁移；原库直到候选验证成功才进入原子切换。"""
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from core.ownership import Ownership, atomic_json
from memory.migrations.recovery import copy_database, recover_switch, snapshot
from memory.migrations.schema import SCHEMA_VERSION
from memory.migrations.steps import upgrade
from memory.migrations.validation import RESTORE_GUIDE


def preview(path):
    path = Path(path).resolve(strict=True)
    facts = snapshot(path)
    return {"database": str(path), "source_facts": facts, "target_version": SCHEMA_VERSION,
            "steps": [[n, n + 1] for n in range(facts["version"], SCHEMA_VERSION)],
            "backup_method": "SQLite online backup (WAL-aware)",
            "switch_method": "SQLite backup destination transaction",
            "rollback": RESTORE_GUIDE, "requires_exclusive_owner": True}


def migrate(path, *, fault=lambda point: None):
    path = Path(path).resolve(strict=True)
    with Ownership(path) as owner:
        journal_path = path.with_name(path.name + ".migration.json")
        recovered = recover_switch(journal_path, path)
        if recovered:
            return recovered
        plan = preview(path)
        if not plan["steps"]:
            return {**plan, "phase": "already_current"}
        return _prepare_and_switch(path, journal_path, plan, owner, fault)


def _prepare_and_switch(path, journal_path, plan, owner, fault):
    directory = path.with_name(path.name + ".migrations") / uuid.uuid4().hex
    directory.mkdir(parents=True, exist_ok=False)
    backup, candidate = directory / "backup.db", directory / "candidate.db"
    journal = {**plan, "phase": "preparing", "backup": str(backup), "candidate": str(candidate)}
    atomic_json(journal_path, journal)
    copy_database(path, backup)
    if snapshot(backup) != plan["source_facts"]:
        raise ValueError("源库在备份期间变化；拒绝切换，保留一致性备份")
    journal["phase"] = "backed_up"
    atomic_json(journal_path, journal)
    fault("after_backup")
    copy_database(backup, candidate)
    with closing(sqlite3.connect(candidate)) as db:
        upgrade(db, plan["source_facts"]["version"], lambda: datetime.now(UTC).isoformat(), fault)
    facts = snapshot(candidate)
    for table, count in plan["source_facts"]["counts"].items():
        if table != "schema_version" and facts["counts"].get(table) != count:
            raise ValueError(f"候选数据行数变化：{table}，禁止切换")
    journal.update(phase="validated", candidate_facts=facts)
    atomic_json(journal_path, journal)
    fault("before_switch")
    owner.assert_held()
    if snapshot(path) != plan["source_facts"]:
        raise ValueError("候选验证后源库发生变化，拒绝覆盖")
    journal["phase"] = "switching"
    atomic_json(journal_path, journal)
    copy_database(candidate, path, must_exist=True,
                  progress=lambda status, remaining, total: fault("switch_page") if remaining else None)
    fault("after_switch")
    if snapshot(path) != facts:
        raise ValueError("切换后验证失败；保留新库与备份，请离线恢复")
    journal["phase"] = "complete"
    atomic_json(journal_path, journal)
    return journal
