"""Vastar runtime component."""

from __future__ import annotations

import hashlib
from pathlib import Path

from core.tools import safety
from core.tools.operation_control import from_context
from core.tools.registry import OpPlan, ToolParamError, ToolSpec

NAME = "edit_file"

DESCRIPTION = ("按「原文本 → 新文本」精确替换编辑文件：old 必须唯一命中（0 处或多处"
               "都拒绝，多处可显式 replace_all=true 全部替换）。编辑前自动备份、原子写入。"
               "成功结果已包含路径/字节/是否覆盖/备份位置/校验值；无需再调用只读工具复核。")
PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "目标文件路径（相对项目根或绝对路径）"},
        "old": {"type": "string", "description": "待替换的原文（必须精确唯一命中）"},
        "new": {"type": "string", "description": "替换后的文本"},
        "replace_all": {"type": "boolean",
                        "description": "old 命中多处时显式放开为全部替换（默认 false）"},
    },
    "required": ["path", "old", "new"],
}


def _require_args(args: dict) -> tuple[str, str]:
    old = args.get("old")
    new = args.get("new")
    if not isinstance(old, str) or not old:
        raise ToolParamError("old 必须是非空字符串（待替换原文）")
    if not isinstance(new, str):
        raise ToolParamError("new 必须是字符串（替换后的文本）")
    return old, new


def _read_text(resolved_path: Path) -> str:
    try:
        return resolved_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ToolParamError(f"非 UTF-8 文本（{exc}）——edit_file 仅支持文本，显式拒绝") from exc


def _count_hits(text: str, old: str, replace_all: bool) -> int:
    count = text.count(old)
    if count == 0:
        raise ToolParamError(
            "old 未命中（0 处）——已拒绝编辑并回显；请核对原文"
            "（可先用 read_file / search_text 查看相关区间）")
    if count > 1 and not replace_all:
        raise ToolParamError(
            f"old 命中 {count} 处（需唯一命中）——已拒绝；确认要全部替换请显式 replace_all=true")
    return count if replace_all else 1


def plan(args: dict, *, resolved_path: Path, resolved_extra=None) -> OpPlan:
    old, new = _require_args(args)
    if not resolved_path.exists():
        raise ToolParamError(f"文件不存在：{resolved_path}")
    text = _read_text(resolved_path)
    replaced = _count_hits(text, old, bool(args.get("replace_all", False)))
    new_text = text.replace(old, new) if args.get("replace_all", False) else \
        text.replace(old, new, 1)
    size = max(len(text.encode("utf-8")), len(new_text.encode("utf-8")))
    return OpPlan(action="edit", targets=[resolved_path],
                  detail=f"替换 {replaced} 处（文件 {size} 字节）",
                  overwrite=True, backup_source=resolved_path,
                  target_count=1, total_bytes=size)


def run(args: dict, *, resolved_path: Path, resolved_extra=None,
        project_root=None, skip=None, op_context=None) -> str:
    control = from_context(op_context)
    control.checkpoint("prepare")
    old, new = _require_args(args)
    if not resolved_path.exists():
        raise ToolParamError(f"文件不存在：{resolved_path}")
    text = _read_text(resolved_path)
    replace_all = bool(args.get("replace_all", False))
    replaced = _count_hits(text, old, replace_all)
    written = safety.atomic_write_text(
        resolved_path, text.replace(old, new, -1 if replace_all else 1), control=control)
    # B10：写后回读校验（预期 hash = 替换后全文 sha256；实际 hash = 落盘回读 sha256）
    expected_text = text.replace(old, new, -1 if replace_all else 1)
    expected = hashlib.sha256(expected_text.encode("utf-8")).hexdigest()
    actual = hashlib.sha256(resolved_path.read_bytes()).hexdigest()
    before = (control.facts.get("before") or {}).get(str(resolved_path), {})
    control.facts["write_proof"] = {"expected_hash": expected, "actual_hash": actual,
                                    "match": expected == actual,
                                    "preexisting_hash": before.get("sha256")}
    if expected != actual:
        raise ToolParamError(
            f"写后校验不一致（预期 {expected[:16]} ≠ 实际 {actual[:16]}）——显式报错（禁静默）")
    backup = (op_context or {}).get("backup_path") or "见审计 backup_path"
    return (f"已编辑 {resolved_path}\n"
            f"动作：编辑（替换 {replaced} 处）· 字节：{written} · 是否覆盖：是（备份 {backup}）\n"
            f"{safety.verification_facts(resolved_path)}\n{safety.NO_RECHECK_NOTE}")


EDIT_FILE_SPEC = ToolSpec(name=NAME, description=DESCRIPTION, parameters=PARAMETERS,
                          level="C", run=run, plan=plan, cooperative=True,
                          effect="write", resource_scope="file", retryable=False,
                          cancel_phase="before_commit", reconcile="hash")
