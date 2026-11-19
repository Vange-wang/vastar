"""Vastar offline operations; explicit paths are resolved from the invocation directory."""

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.maintenance_logging import policy
from core.maintenance_retention import capacity, prune
from core.maintenance_scan import ScanBudget
from core.maintenance_snapshot import restore, safe, snapshot


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["capacity", "prune", "backup", "restore", "preflight"])
    for name, default in (
        ("db", "memory/history.db"),
        ("logs", "logs"),
        ("backups", "backups"),
        ("archive", None),
        ("dest", None),
    ):
        parser.add_argument("--" + name, type=Path, default=Path(default) if default else None)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--confirm", action="store_true")
    group.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-files", type=int, default=100000)
    parser.add_argument("--max-seconds", type=float, default=15)
    args = parser.parse_args(argv)
    if args.max_files < 1 or not math.isfinite(args.max_seconds) or args.max_seconds <= 0:
        parser.error("scan limits must be finite and positive")
    budget = ScanBudget(args.max_files, args.max_seconds)
    db, logs, backups = (safe(p) for p in (args.db, args.logs, args.backups))
    if backups.is_relative_to(db.parent / "artifacts") or backups == db.parent:
        parser.error("backup directory must be separate from data and artifacts")
    cfg = policy()
    if args.action == "capacity":
        result = capacity(db, logs, backups, cfg, budget)
    elif args.action == "preflight":
        dest = safe(args.dest) if args.dest else None
        conflict = bool(
            dest
            and (dest.exists() or dest.is_relative_to(db.parent) or dest.is_relative_to(backups))
        )
        result = {
            "database_exists": db.is_file(),
            "destination": str(dest) if dest else None,
            "data_loss_risk": "existing or overlapping target"
            if conflict
            else "upgrade outcome requires a verified snapshot and restore rehearsal",
            "source_bytes": capacity(db, logs, backups, cfg, budget),
            "protect": [
                str(db),
                str(db.parent / "artifacts"),
                str(db) + ".revocations.json",
                "config/settings.json",
            ],
            "target_conflict": conflict,
            "upgrade_ready": False,
            "next": "Stop Vastar, backup, restore to a fresh directory and verify. Install upgrade to a separate directory.",
        }
    elif args.action == "prune":
        result = prune(db, logs, backups, cfg, args.confirm, budget)
    elif args.action == "backup":
        result = (
            {"snapshot": str(snapshot(db, backups))}
            if args.confirm
            else {"preview_backup": str(db), "to": str(backups)}
        )
    else:
        if not args.dest or not args.archive:
            parser.error("restore requires --archive and --dest")
        result = (
            {"restored": str(restore(args.archive, safe(args.dest)))}
            if args.confirm
            else {"preview_restore": str(args.dest), "would_refuse": args.dest.exists()}
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
