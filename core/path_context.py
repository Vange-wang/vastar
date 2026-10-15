"""B13 路径身份；schema 1 保留安装目录解释，schema 2 显式初始化后使用。"""
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path


class PathContextError(ValueError):
    """路径或身份不完整；不得以新建空库掩盖。"""


@dataclass(frozen=True)
class PathContext:
    app_dir: Path
    data_dir: Path
    project_id: str
    project_root: Path
    cwd: Path
    workspace: Path | None = None
    schema_version: int = 1

    def facts(self):
        return {key: str(value) if isinstance(value, Path) else value
                for key, value in vars(self).items()}

    def prompt_facts(self):
        return "【运行路径事实】相对工具路径以 cwd 为起点；项目外写入仍受权限门约束。\n" + "\n".join(
            f"- {key}: {value}" for key, value in self.facts().items())


def user_config_dir():
    return Path(os.environ.get("VASTAR_USER_DIR") or
                (Path.home() / ".vastar")).resolve()


def read_json(path):
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise PathContextError(f"配置必须是对象：{path}")
    version = value.get("schema_version", 1)
    if type(version) is not int or version not in (1, 2):
        raise PathContextError(f"不支持的路径 schema：{version}（{path}）")
    return value


def discover_project(cwd):
    return next((p for p in (cwd, *cwd.parents)
                 if (p / ".vastar/project.json").is_file()), None)


def read_identity(project):
    path = project / ".vastar/project.json"
    if not path.is_file():
        raise PathContextError(f"项目未初始化：{path}；请显式 --init-project 或迁移")
    identity = read_json(path)
    try:
        uuid.UUID(identity["project_id"])
        data = Path(identity["data_dir"])
    except (KeyError, ValueError, TypeError) as exc:
        raise PathContextError(f"无效项目身份：{path}") from exc
    if identity.get("data_anchor") == "project":
        data = (project / data).resolve()
        if not data.is_relative_to(project):
            raise PathContextError("项目相对 data_dir 越界")
        identity["data_dir"] = str(data)
    if identity.get("schema_version") != 2 or not data.is_absolute():
        raise PathContextError(f"项目身份必须为 schema 2、data_dir 必须为绝对路径或项目锚定：{path}")
    return identity


def require_database(path):
    if not path.is_file():
        raise PathContextError(f"找不到原数据：{path}；拒绝新建空库冒充恢复，请核对路径或备份")


def validate_data_identity(paths):
    owner_file = paths.data_dir / "owner.json"
    if not owner_file.is_file():
        raise PathContextError(f"找不到原数据身份：{owner_file}；请核对数据目录或备份")
    owner = read_json(owner_file)
    if owner.get("schema_version") != 2 or owner.get("project_id") != paths.project_id:
        raise PathContextError(f"数据目录不属于本项目：{paths.data_dir}")
    receipt = paths.data_dir / "migration-result.json"
    if not receipt.is_file() or read_json(receipt).get("status") != "complete":
        raise PathContextError(f"数据初始化/迁移未完成：{paths.data_dir}；保留半成品，请核对迁移记录")


def write_new_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
