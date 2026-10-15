"""显式的一次性路径迁移；源库只读、一致性备份优先、不覆盖既有目标。"""
import json
import shutil
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from core.path_context import PathContextError, read_json, require_database, write_new_json
from memory.migrations.validation import inspect_database


def check_database(path):
    require_database(path)
    try:
        inspect_database(path)
    except ValueError as exc:
        raise PathContextError(str(exc)) from exc
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        version = db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise PathContextError(f"数据库完整性检查失败：{path}")
        return {"schema_version": version,
                "sessions": db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
                "messages": db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]}


def sqlite_backup(source, destination):
    """WAL 感知的一致性备份；不用文件复制伪装 SQLite 备份。"""
    require_database(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb"):
        pass
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(destination)) as dst:
            src.backup(dst)
    _carry_revocations(source, destination)
    return check_database(destination)


def _carry_revocations(source, destination):
    """路径导入须携带权威撤回日志；不覆盖目标已有日志，不把缺失当空日志。"""
    with closing(sqlite3.connect(destination.as_uri() + '?mode=ro', uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_guard'").fetchone():
            return
        expected = db.execute('SELECT journal_id FROM memory_guard WHERE id=1').fetchone()[0]
        if expected is None:
            return  # 尚未首次启动的 schema10；首次持 owner 时创建日志。
        journal = source.with_name(source.name + '.revocations.json')
        if not journal.is_file():
            raise PathContextError('撤回日志缺失；路径迁移拒绝导入')
        value = read_json(journal)
        applied = {r[0] for r in db.execute('SELECT action_id FROM memory_actions')}
        if value.get('journal_id') != expected or not applied.issubset({e['action_id'] for e in value.get('entries', [])}):
            raise PathContextError('撤回日志身份/水位不匹配；路径迁移拒绝导入')
        write_new_json(destination.with_name(destination.name + '.revocations.json'), value)


def migration_preview(source_app, project, data):
    source_app, project, data = (Path(p).resolve() for p in (source_app, project, data))
    config = read_json(source_app / "config/settings.json")
    if config.get("schema_version", 1) != 1:
        raise PathContextError("一次性导入仅接受 schema 1 安装目录")
    source = (source_app / config["memory"]["path"]).resolve()
    facts = check_database(source)
    _validate_destination(project, data)
    resources = [source_app / "memory/_backups", source_app / "memory/_trash",
                 source_app / config["runtime"].get("log_dir", "logs"),
                 source_app / config["prompt"].get("snapshot_dir", "prompts/snapshots")]
    asset_roots = [source_app / config.get(section, {}).get("dir", default)
                   for section, default in (("prompt", "prompts"), ("skills", "skills"))]
    for resource in [*resources, *asset_roots]:
        if data.is_relative_to(resource.resolve()):
            raise PathContextError(f"目标数据目录不得嵌入迁移源资源：{resource}")
    return {"from_schema": 1, "to_schema": 2, "database_schema": facts["schema_version"],
            "source_db": str(source), "project_root": str(project), "data_dir": str(data),
            "source_facts": facts, "resources": [str(p.resolve()) for p in resources],
            "source_unchanged": True, "requires_quiescent_source": True,
            "rollback": "停止新旧进程；先备份升级后整个 data_dir，再使用保留的旧配置/旧库。"
                        "新库留存，不覆盖旧库；两边新增记录不自动合并。"}


def _validate_destination(project, data):
    if not project.is_dir():
        raise PathContextError(f"项目目录不存在：{project}")
    if (project / ".vastar/project.json").exists():
        raise PathContextError("项目已有身份，拒绝重复初始化/导入")
    if (project / ".vastar/settings.json").exists():
        raise PathContextError("目标已有项目配置，拒绝覆盖；先明确迁移配置")
    if data.exists():
        raise PathContextError(f"目标数据目录已存在，拒绝覆盖或重复导入：{data}")
    if project == data or project.is_relative_to(data):
        raise PathContextError("data_dir 不得包含整个项目目录")


def _copy_resource(source, target):
    if not source.exists():
        return
    if source.is_symlink() or getattr(source, "is_junction", lambda: False)():
        raise PathContextError(f"迁移资源含链接，拒绝自动跟随：{source}")
    for path in source.rglob("*"):
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise PathContextError(f"迁移资源含链接，拒绝自动跟随：{path}")
    shutil.copytree(source, target)


def _migrate_config(source_app, project, data):
    """保留模型/预算等原配置；仅显式改路径 schema，资源随目标数据移动。"""
    source_app = Path(source_app).resolve()
    raw = read_json(source_app / "config/settings.json")
    for section, default in (("prompt", "prompts"), ("skills", "skills")):
        source = (source_app / raw.get(section, {}).get("dir", default)).resolve()
        if source.exists():
            _copy_resource(source, data / "resources" / section)
            raw.setdefault(section, {})["dir"] = f"@data/resources/{section}"
    raw["schema_version"] = 2
    raw["memory"]["path"] = "memory/history.db"
    raw["runtime"]["log_dir"] = "logs"
    # 不复制密钥；用户配置目录 .env 或显式 paths.env_file 由操作者选取。
    raw.pop("paths", None)
    write_new_json(data / "migration_backup/settings.json",
                   read_json(source_app / "config/settings.json"))
    write_new_json(project / ".vastar/settings.json", raw)


def initialize_project(project, data, *, source_app=None):
    project, data = Path(project).resolve(), Path(data).resolve()
    _validate_destination(project, data)
    preview = migration_preview(source_app, project, data) if source_app else None
    project_id = str(uuid.uuid4())
    identity = {"schema_version": 2, "project_id": project_id, "data_dir": str(data)}
    data.mkdir(parents=True, exist_ok=False)  # 独占目的目录；半成品保留且拒绝启动/重入。
    journal = {"migration_id": str(uuid.uuid4()), "status": "preparing",
               "from_schema": 1 if preview else None, "to_schema": 2,
               "project_id": project_id, "preview": preview}
    write_new_json(data / "migration-started.json", journal)
    db_path = data / "memory/history.db"
    try:
        if preview:
            backup = data / "migration_backup/history.db"
            facts = sqlite_backup(Path(preview["source_db"]), backup)
            imported = sqlite_backup(backup, db_path)
            if facts != imported:
                raise PathContextError("导入与备份计数不一致")
            targets = ("memory/_backups", "memory/_trash", "logs", "prompt_snapshots")
            for src, target in zip(preview["resources"], targets, strict=True):
                _copy_resource(Path(src), data / target)
            _migrate_config(source_app, project, data)
            journal.update(backup_ref=str(backup), verified=imported)
        else:
            from memory.store import Store
            Store(db_path).close()
            journal["verified"] = check_database(db_path)
        write_new_json(data / "owner.json", identity)
        marker = dict(identity)
        if data.is_relative_to(project):
            marker.update(data_dir=str(data.relative_to(project)), data_anchor="project")
        write_new_json(project / ".vastar/project.json", marker)
        journal["status"] = "complete"
    except Exception as exc:
        journal.update(status="failed", error=str(exc))
        raise
    finally:
        write_new_json(data / "migration-result.json", journal)
    return {**identity, "project_root": str(project), "db_path": str(db_path), **journal}


def format_result(value):
    return json.dumps(value, ensure_ascii=False, indent=2)
