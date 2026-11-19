"""Bounded filesystem measurements for the maintenance CLI."""

import hashlib
import os
import re
import stat
import time

from core.maintenance_snapshot import safe

LOG_SEGMENT = re.compile(r"vastar\.log\.\d+-[a-f0-9]{32}")


class ScanLimit(Exception):
    pass


class ScanBudget:
    def __init__(self, max_files=100000, max_seconds=15):
        self.max_files = max_files
        self.deadline = time.monotonic() + max_seconds
        self.visited = 0

    def expired(self):
        return time.monotonic() >= self.deadline

    def check(self):
        if self.expired():
            raise ScanLimit("max-seconds")

    def visit(self):
        self.check()
        if self.visited >= self.max_files:
            raise ScanLimit("max-files")
        self.visited += 1

    def entries(self, path):
        if safe(path).is_dir():
            with os.scandir(path) as entries:
                for entry in entries:
                    self.visit()
                    yield entry

    def checksum(self, stream):
        h = hashlib.sha256()
        while True:
            self.check()
            chunk = stream.read(1024 * 1024)
            if not chunk:
                return h.hexdigest()
            h.update(chunk)


def capacity(db, logs, backups, cfg, budget=None):
    budget = budget or ScanBudget()
    amounts = {}
    for key, limit in (
        ("database", "database_warn_bytes"),
        ("logs", "log_bytes"),
        ("artifacts", "artifact_warn_bytes"),
        ("backups", "backup_warn_bytes"),
    ):
        total = 0
        reason = None
        try:
            if key == "database":
                for suffix in ("", "-wal", "-shm"):
                    budget.visit()
                    path = safe(db.with_name(db.name + suffix))
                    if path.is_file():
                        total += path.stat().st_size
            else:
                dirs = [
                    {"logs": logs, "artifacts": db.parent / "artifacts", "backups": backups}[key]
                ]
                while dirs:
                    for entry in budget.entries(dirs.pop()):
                        if (
                            key == "logs"
                            and entry.name != "vastar.log"
                            and not LOG_SEGMENT.fullmatch(entry.name)
                        ):
                            continue
                        info = entry.stat(follow_symlinks=False)
                        if (
                            getattr(info, "st_file_attributes", 0)
                            & stat.FILE_ATTRIBUTE_REPARSE_POINT
                        ):
                            continue
                        if stat.S_ISREG(info.st_mode):
                            total += info.st_size
                        elif key != "logs" and stat.S_ISDIR(info.st_mode):
                            dirs.append(entry.path)
            budget.check()
        except (ScanLimit, OSError) as exc:
            reason = str(exc)
        over = total > cfg[limit]
        amounts[key] = {
            "bytes": total,
            "limit": cfg[limit],
            "over": over if over or not reason else None,
            "complete": reason is None,
            "reason": reason,
        }
    return amounts
