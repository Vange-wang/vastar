"""Vastar runtime component."""

from __future__ import annotations

import hashlib
from pathlib import Path

from core.tools import safety
from core.tools.operation_control import from_context
from core.tools.registry import OpPlan, ToolParamError, ToolSpec

NAME = "write_file"

DESCRIPTION = ("新建或整体覆盖一个 UTF-8 文本文件。覆盖已存在文件必须显式"
               " mode=\"overwrite\"（覆盖前自动备份、原子写入）；新建无需 mode。"
               "成功结果已包含路径/字节/是否覆盖/备份位置/校验值；无需再调用只读工具复核。")
PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "目标文件路径（相对项目根或绝对路径）"},
        "content": {"type": "string", "description": "完整文件内容（UTF-8 文本）"},
        "mode": {"type": "string",
                 "description": "覆盖已存在文件时必须为 \"overwrite\"；新建可省略"},
    },
    "required": ["path", "content"],
}


def _require_content(args: dict) -> str:
    content = args.get("content")
    if not isinstance(content, str):
        raise ToolParamError("content 必须是字符串（完整文件内容）")
    return content


def _require_overwrite_mode(resolved_path: Path, args: dict) -> bool:
    """已存在目标必须显式 mode="overwrite"（任务单 ①A 关键约束）。返回是否为覆盖。"""
    if resolved_path.exists() and args.get("mode") != "overwrite":
        raise ToolParamError(
            f"目标已存在（{resolved_path.name}）：整体覆盖必须显式 mode=\"overwrite\"")
    return resolved_path.exists()


def plan(args: dict, *, resolved_path: Path, resolved_extra=None) -> OpPlan:
    content = _require_content(args)
    overwrite = _require_overwrite_mode(resolved_path, args)
    size = len(content.encode("utf-8"))
    return OpPlan(
        action="edit" if overwrite else "create",
        targets=[resolved_path],
        detail=f"{'覆盖' if overwrite else '新建'} {size} 字节",
        overwrite=overwrite,
        backup_source=resolved_path if overwrite else None,
        target_count=1,
        total_bytes=size,
    )


def run(args: dict, *, resolved_path: Path, resolved_extra=None,
        project_root=None, skip=None, op_context=None) -> str:
    control = from_context(op_context)
    control.checkpoint("prepare")
    content = _require_content(args)
    overwrite = _require_overwrite_mode(resolved_path, args)
    written = safety.atomic_write_text(resolved_path, content, control=control)
    # B10：写后回读校验（预期 hash = 拟写内容 sha256；实际 hash = 落盘回读 sha256）
    expected = hashlib.sha256(content.encode("utf-8")).hexdigest()
    actual = hashlib.sha256(resolved_path.read_bytes()).hexdigest()
    before = (control.facts.get("before") or {}).get(str(resolved_path), {})
    control.facts["write_proof"] = {"expected_hash": expected, "actual_hash": actual,
                                    "match": expected == actual,
                                    "preexisting_hash": before.get("sha256")}
    if expected != actual:
        raise ToolParamError(
            f"写后校验不一致（预期 {expected[:16]} ≠ 实际 {actual[:16]}）——显式报错（禁静默）")
    backup = (op_context or {}).get("backup_path")
    cover = f"是（备份 {backup}）" if overwrite and backup else ("是" if overwrite else "否")
    action = "覆盖" if overwrite else "新建"
    return (f"已写入（{action}）{resolved_path}\n"
            f"动作：{action} · 字节：{written} · 是否覆盖：{cover}\n"
            f"{safety.verification_facts(resolved_path)}\n{safety.NO_RECHECK_NOTE}")


WRITE_FILE_SPEC = ToolSpec(name=NAME, description=DESCRIPTION, parameters=PARAMETERS,
                           level="C", run=run, plan=plan, cooperative=True,
                           effect="write", resource_scope="file", retryable=True,
                           cancel_phase="before_commit", reconcile="hash")
