"""Vastar runtime component."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from core.tools.registry import ToolParamError, ToolSpec

NAME = "stat_path"

DESCRIPTION = ("查看路径的存在性 / 类型 / 大小 / 修改时间（只读）。路径不存在时明确"
               "回显「不存在」（属正常答案，不是错误）。")
PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "目标路径（相对项目根或绝对路径）"},
    },
    "required": ["path"],
}


def run(args: dict, *, resolved_path: Path, resolved_extra=None,
        project_root=None, skip=None, op_context=None) -> str:
    if not resolved_path.exists():
        return f"不存在：{resolved_path}"
    try:
        st = resolved_path.stat()
    except PermissionError:
        raise ToolParamError(
            f"无访问权限：{resolved_path}（该目录可能属于其他账户或受系统保护）") from None
    except FileNotFoundError:
        raise ToolParamError(f"路径不存在：{resolved_path}") from None
    except OSError as exc:
        raise ToolParamError(
            f"无法读取状态：{resolved_path}（{type(exc).__name__}）——已显式失败，不静默") from exc
    mtime = datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")
    if resolved_path.is_dir():
        try:
            entries = sum(1 for _ in resolved_path.iterdir())
        except PermissionError:
            raise ToolParamError(
                f"无访问权限：{resolved_path}（该目录可能属于其他账户或受系统保护）") from None
        except OSError as exc:
            raise ToolParamError(
                f"目录不可读：{resolved_path}（{type(exc).__name__}）——已显式失败，不静默") from exc
        return f"目录：{resolved_path}\n直接条目 {entries} 个 · 修改时间 {mtime}"
    return f"文件：{resolved_path}\n大小 {st.st_size} 字节 · 修改时间 {mtime}"


STAT_PATH_SPEC = ToolSpec(name=NAME, description=DESCRIPTION, parameters=PARAMETERS,
                          level="B", run=run, cooperative=True,
                          effect="read", resource_scope="path", retryable=True,
                          cancel_phase="any", reconcile="reread")
