"""Offline snapshot with the same kernel lock as the application and SQLite online backup."""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import uuid
import zipfile
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath

from core.file_lock import FileLock


def safe(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or (
            part.exists()
            and getattr(part.stat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise ValueError(f"linked path refused: {part}")
    return path


def digest(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


@contextmanager
def offline(db):
    safe(db)
    if not db.is_file() or db.stat().st_nlink != 1:
        raise ValueError("require existing uniquely named database")
    lock = FileLock(safe(db.with_name(db.name + ".instance.lock")))
    lock.acquire()
    try:
        yield
    finally:
        lock.close()


def indexed(db, folder):
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as con:
        if con.execute("pragma quick_check").fetchone() != ("ok",):
            raise ValueError("database integrity check failed")
        for aid, version, expected in con.execute("select artifact_id,version,hash from artifacts"):
            if not re.fullmatch(r"art-[a-f0-9]+", aid) or type(version) is not int:
                raise ValueError("invalid artifact index")
            if digest(safe(folder / f"{aid}.v{version}.blob")) != expected:
                raise ValueError(f"artifact hash mismatch: {aid}")


def snapshot(db, backups):
    with offline(db):
        safe(backups).mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=backups) as tmp:
            stage = Path(tmp)
            data = stage / "data"
            data.mkdir()
            with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as src:
                with closing(sqlite3.connect(data / db.name)) as dest:
                    src.backup(dest)
            files = [
                db.with_name(db.name + suffix)
                for suffix in (".revocations.json", ".migration.json")
            ]
            files += [db.parent / n for n in ("owner.json", "migration-result.json")]
            for src in files:
                if safe(src).is_file():
                    shutil.copyfile(src, data / src.name)
            artifacts = safe(db.parent / "artifacts")
            for src in artifacts.rglob("*"):
                if safe(src).is_file():
                    target = data / src.relative_to(db.parent)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src, target)
            indexed(data / db.name, data / "artifacts")
            entries = {
                p.relative_to(data).as_posix(): digest(p) for p in data.rglob("*") if p.is_file()
            }
            name = f"vastar-{uuid.uuid4().hex}.zip"
            with zipfile.ZipFile(stage / name, "x") as arc:
                for key in sorted(entries):
                    arc.write(data / key, key)
                arc.writestr(
                    "manifest.json",
                    json.dumps(
                        {
                            "version": 1,
                            "source_database": str(db.resolve()),
                            "db": db.name,
                            "created": datetime.now(UTC).isoformat(),
                            "files": entries,
                        }
                    ),
                )
            (stage / name).rename(backups / name)
    return backups / name


def restore(archive, dest):
    safe(dest)
    if dest.exists():
        raise FileExistsError(f"restore requires new destination: {dest}")
    with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
        stage = Path(tmp) / "data"
        stage.mkdir()
        with zipfile.ZipFile(archive) as arc:
            manifest = json.loads(arc.read("manifest.json"))
            entries = manifest["files"]
            names = arc.namelist()
            if (
                manifest["version"] != 1
                or len(set(k.casefold() for k in names)) != len(names)
                or set(names) != set(entries) | {"manifest.json"}
                or manifest["db"] not in entries
            ):
                raise ValueError("invalid snapshot manifest")
            for name, expected in entries.items():
                parts = name.split("/")
                if any(
                    not k
                    or k in (".", "..")
                    or PureWindowsPath(k).is_reserved()
                    or re.search(r'[\\:\x00-\x1f<>"|?*]', k)
                    or k.endswith((" ", "."))
                    for k in parts
                ):
                    raise ValueError("invalid snapshot path")
                target = stage.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with arc.open(name) as src, target.open("xb") as out:
                    shutil.copyfileobj(src, out)
                if digest(target) != expected:
                    raise ValueError(f"snapshot checksum mismatch: {name}")
        indexed(stage / manifest["db"], stage / "artifacts")
        # Windows rename refuses an existing destination, including an empty directory.
        if dest.exists():
            raise FileExistsError(dest)
        os.rename(stage, dest)
    return dest
