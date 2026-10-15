"""Vastar runtime component."""

from __future__ import annotations

from pathlib import Path

from core.tools import safety
from core.tools.operation_control import from_context
from core.tools.registry import OpPlan, ToolParamError, ToolSpec

NAME = "mkdir"

DESCRIPTION = ("创建目录（可 parents=true 逐级创建）；幂等——已存在明确回显。"
               "成功结果已包含路径/条目数/校验值；无需再调用只读工具复核。")
PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "目标目录路径（相对项目根或绝对路径）"},
        "parents": {"type": "boolean", "description": "逐级创建缺失的父目录（默认 true）"},
    },
    "required": ["path"],
}


def plan(args: dict, *, resolved_path: Path, resolved_extra=None) -> OpPlan:
    if resolved_path.exists() and not resolved_path.is_dir():
        raise ToolParamError(f"路径已存在且是文件，不能在其上建目录：{resolved_path}")
    exists = resolved_path.is_dir()
    return OpPlan(action="create", targets=[resolved_path],
                  detail="目录已存在（幂等）" if exists else "新建目录",
                  target_count=1, total_bytes=0)


def _count_entries(path: Path) -> int:
    try:
        return sum(1 for _ in path.iterdir())
    except OSError:
        return 0


def run(args: dict, *, resolved_path: Path, resolved_extra=None,
        project_root=None, skip=None, op_context=None) -> str:
    control = from_context(op_context)
    control.checkpoint("prepare")
    if resolved_path.is_dir():
        entries = _count_entries(resolved_path)
        return (f"目录已存在（幂等，未变更）：{resolved_path}\n"
                f"动作：新建目录（已存在） · 条目数：{entries} · 是否覆盖：否\n"
                f"{safety.verification_facts(resolved_path)}\n{safety.NO_RECHECK_NOTE}")
    parents = True if args.get("parents") is None else bool(args.get("parents"))
    control.commit(lambda: resolved_path.mkdir(parents=parents, exist_ok=True))
    entries = _count_entries(resolved_path)
    return (f"已创建目录：{resolved_path}\n"
            f"动作：新建目录 · 条目数：{entries} · 是否覆盖：否\n"
            f"{safety.verification_facts(resolved_path)}\n{safety.NO_RECHECK_NOTE}")


MKDIR_SPEC = ToolSpec(name=NAME, description=DESCRIPTION, parameters=PARAMETERS,
                      level="C", run=run, plan=plan, cooperative=True,
                      effect="write", resource_scope="path", retryable=True,
                      cancel_phase="before_commit", reconcile="none")
