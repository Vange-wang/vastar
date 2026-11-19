"""Conservative, offline retention for managed immutable blobs and named segments."""

import json
import re
import sqlite3
import time
import zipfile
from contextlib import closing
from datetime import datetime
from pathlib import Path

from core.maintenance_scan import LOG_SEGMENT, ScanBudget, ScanLimit
from core.maintenance_scan import capacity as capacity
from core.maintenance_snapshot import offline, safe


def artifact_reason(con, row, rank, cfg, pending, now):
    aid, version, uri, retention, created = row
    if pending:
        return "pending/active run or unsettled operation"
    if not re.fullmatch(rf"artifact://[^/]+/{re.escape(aid)}/v{version}#[a-f0-9]{{12}}", uri):
        return "invalid delivery reference"
    if con.execute(
        "select 1 from task_steps where instr(evidence_refs,?) limit 1", (aid,)
    ).fetchone():
        return "task evidence"
    if rank < cfg["artifact_count"]:
        return "recent count"
    match = re.fullmatch(r"(\d+)d", retention)
    if not match or datetime.fromisoformat(created).timestamp() + int(match[1]) * 86400 > now:
        return "retention"

    if con.execute("select 1 from checkpoints where instr(body,?) limit 1", (aid,)).fetchone():
        return "checkpoint"
    for table, content, stamp in (
        ("messages", "content", "created_at"),
        ("events", "payload", "timestamp_utc"),
        ("runs", "coalesce(result_json,'') || coalesce(config_snapshot,'')", "updated_at"),
        ("tool_operations", "facts || coalesce(receipt,'')", "updated_at"),
    ):
        sql = f"select 1 from {table} where instr({content},?) and (unixepoch({stamp}) >= ? or unixepoch({stamp}) is null) limit 1"
        if con.execute(sql, (aid, now - cfg["reference_days"] * 86400)).fetchone():
            return "recent run reference"
    delivered = con.execute(
        """select 1 from messages m join runs r on r.run_id=m.run_id
        where m.role='assistant' and m.status='complete' and r.terminal_state='COMPLETED'
        and instr(m.content,?) limit 1""",
        (uri,),
    ).fetchone()
    return None if delivered else "delivery unconfirmed"


def unsettled(con):
    pending = con.execute("select 1 from runs where terminal_state is null limit 1").fetchone()
    pending = (
        pending
        or con.execute(
            "select 1 from tool_operations where state not in ('committed','not_committed') limit 1"
        ).fetchone()
    )
    pending = (
        pending
        or con.execute(
            "select 1 from pending_inputs where state in ('pending','queued') limit 1"
        ).fetchone()
    )
    return pending


def expired_backups(db, backups, count, skipped, budget):
    verified = []
    for entry in budget.entries(backups):
        p = Path(entry.path)
        if not re.fullmatch(r"vastar-[a-f0-9]{32}\.zip", p.name):
            continue
        try:
            with zipfile.ZipFile(safe(p)) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                entries = manifest["files"]
                if (
                    manifest.get("source_database") != str(db.resolve())
                    or manifest["version"] != 1
                    or manifest["db"] not in entries
                    or set(archive.namelist()) != set(entries) | {"manifest.json"}
                ):
                    raise ValueError("unrecognized backup")
                for name, expected in entries.items():
                    budget.visit()
                    with archive.open(name) as stream:
                        if budget.checksum(stream) != expected:
                            raise ValueError("bad checksum")
            verified.append((datetime.fromisoformat(manifest["created"]).timestamp(), p))
        except (OSError, KeyError, TypeError, ValueError, zipfile.BadZipFile) as exc:
            skipped.append([str(p), f"backup retained: {type(exc).__name__}"])
    return [p for _, p in sorted(verified, reverse=True)[count:]]


def plan(con, db, logs, backups, cfg, budget, planned, skipped):
    now = time.time()
    pending = unsettled(con)
    rows = con.execute(
        "select artifact_id,version,uri,retention,created_at,hash from artifacts order by created_at desc,artifact_id,version desc"
    )
    artifacts = safe(db.parent / "artifacts")
    known = set()
    for rank, (*row, expected) in enumerate(rows):
        budget.visit()
        aid, version = row[:2]
        if not re.fullmatch(r"art-[a-f0-9]+", aid) or type(version) is not int:
            raise ValueError("invalid artifact name")
        path = artifacts / f"{aid}.v{version}.blob"
        known.add(path)
        reason = artifact_reason(con, row, rank, cfg, pending, now)
        if not reason:
            reason = "missing or changed blob"
            if safe(path).is_file():
                with path.open("rb") as stream:
                    if budget.checksum(stream) == expected:
                        reason = None
        if reason:
            skipped.append([str(path), reason])
            continue
        planned.append((path, (aid, version)))
    for entry in budget.entries(db.parent / "artifacts"):
        p = Path(entry.path)
        if p not in known:
            skipped.append([str(p), "unindexed/unknown; retained"])
    active = safe(logs / "vastar.log")
    total = active.stat().st_size if active.exists() else 0
    segments = sorted(
        (safe(entry.path) for entry in budget.entries(logs) if LOG_SEGMENT.fullmatch(entry.name)),
        reverse=True,
    )
    candidates = []
    for i, p in enumerate(segments):
        budget.check()
        total += p.stat().st_size
        if (
            i >= cfg["log_segments"]
            or total > cfg["log_bytes"]
            or now - p.stat().st_mtime > cfg["log_days"] * 86400
        ):
            candidates.append(p)
    candidates.extend(expired_backups(db, backups, cfg["backup_count"], skipped, budget))
    planned.extend((p, None) for p in candidates)
    budget.check()


def prune(db, logs, backups, cfg, apply=False, budget=None):
    planned, skipped = [], []
    budget = budget or ScanBudget()
    with (
        offline(db),
        closing(
            sqlite3.connect(db.as_uri() + ("?mode=rw" if apply else "?mode=ro"), uri=True)
        ) as con,
    ):
        con.set_progress_handler(budget.expired, 1000)
        try:
            plan(con, db, logs, backups, cfg, budget, planned, skipped)
        except (ScanLimit, sqlite3.OperationalError) as exc:
            if (
                isinstance(exc, sqlite3.OperationalError)
                and exc.sqlite_errorcode != sqlite3.SQLITE_INTERRUPT
            ):
                raise
            return {
                "complete": False,
                "reason": str(exc),
                "visited": budget.visited,
                "applied": False,
                "delete": [],
                "skip": skipped,
            }
        finally:
            con.set_progress_handler(None, 0)
        if apply:
            for path, artifact in planned:
                if artifact:
                    con.execute("delete from artifacts where artifact_id=? and version=?", artifact)
                    con.commit()
                path.unlink()
    return {
        "complete": True,
        "visited": budget.visited,
        "applied": apply,
        "delete": [str(path) for path, _ in planned],
        "skip": skipped,
    }
