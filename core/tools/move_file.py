"""Vastar runtime component."""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

from core.tools import impact_scan, safety
from core.tools.operation_control import from_context
from core.tools.registry import OpPlan, ToolParamError, ToolSpec

NAME = "move_file"

DESCRIPTION = ("移动 / 重命名文件或目录（path=源，dest=目标）。目标文件已存在 → 覆盖"
               "（覆盖前自动备份）；目标目录已存在 → 拒绝（不支持合并）；跨盘移动 = "
               "先复制到临时名、校验成功后才删源，中途失败保留源并清理临时文件。"
               "成功结果已包含路径/字节或条目数/是否覆盖/备份位置/校验值；无需再调用只读工具复核。")
PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "源路径（相对项目根或绝对路径）"},
        "dest": {"type": "string", "description": "目标路径（相对项目根或绝对路径）"},
    },
    "required": ["path", "dest"],
}

_TMP_PREFIX = ".vastar-move-tmp-"


def _require_dest(resolved_extra: dict | None) -> Path:
    dest = (resolved_extra or {}).get("dest")
    if dest is None:
        raise ToolParamError("缺少 dest 参数（目标路径）")
    return dest


def _same_volume(src: Path, dest: Path) -> bool:
    return os.path.splitdrive(str(src))[0].lower() == os.path.splitdrive(str(dest))[0].lower()


def _plan_facts(src: Path, dest: Path) -> tuple[bool, int, int, bool, bool]:
    """返回 (overwrite, count, total_bytes, recursive, dest_exists)。"""
    dest_exists = dest.exists()
    if dest_exists and src.is_dir():
        raise ToolParamError(f"目标已存在且源是目录——move 不支持目录合并：{dest}，请更换目标名")
    overwrite = dest_exists and src.is_file() and dest.is_file()
    recursive = src.is_dir()
    if recursive:
        count, total = safety.dir_stats(src)
    else:
        count, total = 1, src.stat().st_size
    if overwrite:
        count += 1                       # 被覆盖目标同属受影响集合（将备份 + 替换）
        total += dest.stat().st_size
    return overwrite, count, total, recursive, dest_exists


def plan(args: dict, *, resolved_path: Path, resolved_extra=None) -> OpPlan:
    dest = _require_dest(resolved_extra)
    src = resolved_path
    if not src.exists():
        raise ToolParamError(f"源不存在：{src}")
    if src == dest:
        raise ToolParamError(f"源与目标相同：{src}")
    # B10：大小写/规范路径——目标不存在但同目录有大小写异写同名项（Windows 会实际命中该项）
    conflict = impact_scan.case_conflict_for(dest)
    if conflict is not None:
        raise ToolParamError(
            f"目标名与现有项仅大小写不同（现有 {conflict}）——显式拒绝；"
            "Windows 大小写不敏感，写入会实际命中既有项，请更换目标名")
    overwrite, count, total, recursive, _ = _plan_facts(src, dest)
    scale = f"目录 {count} 项" if recursive else f"{total} 字节"
    detail = f"{src.name} → {dest.name}（{scale}{'，覆盖目标（将自动备份）' if overwrite else ''}）"
    targets = [src, dest]
    if recursive:
        # B10：目录移动展开受影响后代全集——嵌套硬拒项才能被执行层整批一票否决
        
        entries = sorted(src.rglob("*"))
        links = [str(p) for p in entries if impact_scan.is_link(p)]
        if links:
            detail += f"，含链接/junction {len(links)} 个（{'; '.join(links[:3])}）"
        targets = [src, *entries, dest]
    return OpPlan(action="edit" if overwrite else "move",
                  targets=targets, detail=detail,
                  overwrite=overwrite,
                  backup_source=dest if overwrite else None,
                  target_count=count, total_bytes=total, recursive=recursive)


def _verify_and_commit(tmp: Path, src: Path, dest: Path, control) -> None:
    """校验临时副本与源一致后才就位（os.replace / os.rename 同卷原子），然后删源。"""
    if src.is_dir():
        if safety.dir_stats(tmp) != safety.dir_stats(src):
            raise ToolParamError("跨盘复制校验不一致（条目/字节不齐）——已保留源")
        def commit_directory():
            os.rename(tmp, dest)
            shutil.rmtree(src)
        control.commit(commit_directory)
    else:
        if tmp.stat().st_size != src.stat().st_size:
            raise ToolParamError("跨盘复制校验不一致（大小不齐）——已保留源")
        def commit_file():
            os.replace(tmp, dest)
            src.unlink()
        control.commit(commit_file)


def _move_cross_volume(src: Path, dest: Path, control) -> str:
    tmp = dest.parent / f"{_TMP_PREFIX}{uuid.uuid4().hex[:8]}"
    try:
        control.checkpoint("before_temp_write")
        if src.is_dir():
            shutil.copytree(src, tmp)
        else:
            shutil.copy2(src, tmp)
        _verify_and_commit(tmp, src, dest, control)
    except BaseException:
        if tmp.is_dir():
            shutil.rmtree(tmp)
        elif tmp.exists():
            tmp.unlink(missing_ok=True)
        raise
    return f"已跨盘移动（复制→校验→删源）：{src} → {dest}"


def run(args: dict, *, resolved_path: Path, resolved_extra=None,
        project_root=None, skip=None, op_context=None) -> str:
    control = from_context(op_context)
    control.checkpoint("prepare")
    dest = _require_dest(resolved_extra)
    src = resolved_path
    if not src.exists():
        raise ToolParamError(f"源不存在：{src}")
    overwrite = src.is_file() and dest.is_file() and dest.exists()
    cross = not _same_volume(src, dest)
    with safety.prepared_parent(dest, control):
        if cross:
            _move_cross_volume(src, dest, control)
        elif src.is_dir():
            control.commit(lambda: os.rename(src, dest))
        else:
            control.commit(lambda: os.replace(src, dest))
    backup = (op_context or {}).get("backup_path")
    cover = f"是（备份 {backup}）" if overwrite and backup else ("是" if overwrite else "否")
    if dest.is_dir():
        count, total = safety.dir_stats(dest)
        scale = f"条目数：{count}（{total} 字节）"
    else:
        scale = f"字节：{dest.stat().st_size}"
    verb = "移动并覆盖目标" if overwrite else "移动"
    how = "跨盘复制→校验→删源" if cross else "同卷"
    return (f"已移动：{src} → {dest}\n"
            f"动作：{verb} · {scale} · 是否覆盖：{cover}（{how}）\n"
            f"{safety.verification_facts(dest)}\n{safety.NO_RECHECK_NOTE}")


MOVE_FILE_SPEC = ToolSpec(name=NAME, description=DESCRIPTION, parameters=PARAMETERS,
                          level="C", run=run, plan=plan, cooperative=True,
                          effect="move", resource_scope="path", retryable=False,
                          cancel_phase="before_commit", reconcile="stats")
