"""Vastar runtime component."""
import os
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from core.path_context import (
    PathContext,
    PathContextError,
    discover_project,
    read_identity,
    read_json,
    require_database,
    user_config_dir,
    validate_data_identity,
)
from core.path_migration import check_database


@dataclass
class LaunchConfig:
    raw: dict
    paths: PathContext
    config_path: Path
    sources: list[str]
    db_path: Path
    log_dir: Path
    env_file: Path


def merge(base, overlay):
    result = deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _absolute(value, base):
    path = Path(value)
    return (base / path).resolve()


def _normalize(layer, base):
    """schema 2 覆盖件中资源路径相对该件目录；状态路径相对 data_dir。"""
    result = deepcopy(layer)
    for section, key in (("prompt", "dir"), ("skills", "dir"),
                         ("paths", "env_file"), ("paths", "data_dir")):
        if key in result.get(section, {}):
            value = result[section][key]
            if not str(value).startswith("@data/"):
                result[section][key] = str(_absolute(value, base))
    return result


def _layers(args, app, project):
    default = app / "config/settings.json"
    raw = read_json(default)
    raw = _normalize(raw, app)
    sources = [str(default)]
    locations = [user_config_dir() / "settings.json", project / ".vastar/settings.json"]
    explicit = getattr(args, "config", None) or os.environ.get("VASTAR_CONFIG")
    if explicit:
        locations.append(_absolute(explicit, Path.cwd()))
    for path in locations:
        if not path.is_file():
            if explicit and path == locations[-1]:
                raise PathContextError(f"配置不存在：{path}")
            continue
        layer = read_json(path)
        if layer.get("schema_version") != 2:
            raise PathContextError(f"新路径模式覆盖配置须声明 schema_version: 2：{path}")
        raw = merge(raw, _normalize(layer, path.parent))
        sources.append(str(path))
    raw["schema_version"] = 2
    return raw, sources


def _legacy(args, app):
    config = _absolute(getattr(args, "config", None) or
                       os.environ.get("VASTAR_CONFIG") or "config/settings.json", app)
    raw = read_json(config)
    if raw.get("schema_version", 1) != 1:
        raise PathContextError("schema 2 配置需要 --project 或已有 .vastar/project.json")
    db = _absolute(raw["memory"]["path"], app)
    if getattr(args, "continue_session", False):
        require_database(db)
    if db.exists():
        check_database(db)
    paths = PathContext(app, db.parent, "legacy", app, app)
    return LaunchConfig(raw, paths, config, [str(config)], db,
                        _absolute(raw["runtime"].get("log_dir", "logs"), app), app / ".env")


def load_launch_config(args, app_dir):
    app, invocation = Path(app_dir).resolve(), Path.cwd().resolve()
    project_arg = getattr(args, "project", None)
    project = _absolute(project_arg, invocation) if project_arg else discover_project(invocation)
    if project is None:
        if any(getattr(args, key, None) for key in ("data_dir", "cwd", "workspace")):
            raise PathContextError("--data-dir/--cwd/--workspace 需要 --project 或已初始化项目")
        return _legacy(args, app)
    identity = read_identity(project)
    raw, sources = _layers(args, app, project)
    data = _absolute(getattr(args, "data_dir", None) or
                     raw.get("paths", {}).get("data_dir") or identity["data_dir"], invocation)
    workspace_arg = getattr(args, "workspace", None)
    workspace = _absolute(workspace_arg, invocation) if workspace_arg else None
    cwd = _absolute(getattr(args, "cwd", None) or
                    (workspace or (invocation if invocation.is_relative_to(project) else project)),
                    invocation)
    if not project.is_dir() or not cwd.is_dir() or workspace and not workspace.is_dir():
        raise PathContextError("project_root/cwd/workspace 必须为存在的目录")
    if workspace and not cwd.is_relative_to(workspace):
        raise PathContextError("指定 workspace 时 cwd 必须在执行副本内")
    paths = PathContext(app, data, identity["project_id"], project, cwd, workspace, 2)
    validate_data_identity(paths)
    for section in ("prompt", "skills"):
        value = raw.get(section, {}).get("dir", "")
        if str(value).startswith("@data/"):
            resource = _absolute(value[6:], data)
            if not resource.is_relative_to(data):
                raise PathContextError("@data 资源路径越界")
            raw[section]["dir"] = str(resource)
    db = _absolute(raw["memory"]["path"], data)
    log = _absolute(raw["runtime"].get("log_dir", "logs"), data)
    if not db.is_relative_to(data) or not log.is_relative_to(data):
        raise PathContextError("schema 2 的数据库与日志必须位于 data_dir 内")
    require_database(db)
    check_database(db)
    env = Path(raw.get("paths", {}).get("env_file", user_config_dir() / ".env"))
    raw["resolved_paths"] = paths.facts()
    raw["config_sources"] = sources
    return LaunchConfig(raw, paths, Path(sources[-1]), sources, db, log, env)
