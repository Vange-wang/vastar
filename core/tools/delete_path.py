"""Vastar runtime component."""

from __future__ import annotations

from pathlib import Path

from core.tools import impact_scan, safety
from core.tools.operation_control import from_context
from core.tools.registry import OpPlan, ToolParamError, ToolSpec

NAME = "delete_path"

DESCRIPTION = ("删除文件或目录，必须可逆：优先移入系统回收站；不可用则移入项目内"
               " memory/_trash/ 时间戳目录（保留原相对结构）。返回结果含可恢复路径，"
               "把该路径移回原位置即可恢复。绝不硬删。"
               "成功结果已包含路径/字节或条目数/可恢复位置/校验值；无需再调用只读工具复核。")
PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "要删除的路径（相对项目根或绝对路径）"},
    },
    "required": ["path"],
}


def plan(args: dict, *, resolved_path: Path, resolved_extra=None) -> OpPlan:
    if not resolved_path.exists():
        raise ToolParamError(f"路径不存在：{resolved_path}")
    if resolved_path.is_dir():
        count, total = safety.dir_stats(resolved_path)
        # 受影响全集 = 目录自身 + 展开条目：嵌套的硬拒项（如目录内的受保护文件名）
        
        entries = sorted(resolved_path.rglob("*"))
        # B10：影响面事实——链接/junction 后代显式点名（不跟随、不探测目标内容）
        links = [str(p) for p in entries if impact_scan.is_link(p)]
        detail = f"目录，共 {count} 项 / {total} 字节"
        if links:
            detail += f"，含链接/junction {len(links)} 个（{'; '.join(links[:3])}）"
        return OpPlan(action="delete", targets=[resolved_path, *entries],
                      detail=detail,
                      reversible_delete=True, needs_recycle=True,
                      target_count=count, total_bytes=total, recursive=True)
    size = resolved_path.stat().st_size
    return OpPlan(action="delete", targets=[resolved_path],
                  detail=f"文件，{size} 字节",
                  reversible_delete=True, needs_recycle=True,
                  target_count=1, total_bytes=size)


def run(args: dict, *, resolved_path: Path, resolved_extra=None,
        project_root=None, skip=None, op_context=None) -> str:
    control = from_context(op_context)
    control.checkpoint("prepare")
    if project_root is None:
        raise ToolParamError("缺少 project_root（执行层注入）")
    if not resolved_path.exists():
        raise ToolParamError(f"路径不存在：{resolved_path}")
    # 校验事实在删除前取（sha256/mtime = 删除前凭证；目录给条目数/字节）
    if resolved_path.is_dir():
        count, total = safety.dir_stats(resolved_path)
        scale = f"条目数：{count}（{total} 字节）"
    else:
        scale = f"字节：{resolved_path.stat().st_size}"
    facts = safety.verification_facts(resolved_path)
    recover = control.commit(lambda: _delete(resolved_path, project_root,
                                            (op_context or {}).get("data_dir")))
    control.facts["recovery_path"] = str(recover)
    # B10：恢复凭证结构化（恢复位置 + 原路径 + 保留期限，如实声明）
    control.facts["recovery"] = safety.recovery_credential(recover, resolved_path)
    if recover == "系统回收站":
        return (f"已删除（移入系统回收站）：{resolved_path}\n"
                f"动作：删除 · {scale} · 可恢复位置：系统回收站（可从回收站还原）\n"
                f"{facts}（删除前取值）\n{safety.NO_RECHECK_NOTE}")
    return (f"已删除（可逆）：原 {resolved_path}\n"
            f"动作：删除 · {scale} · 可恢复位置：{recover}（移回原位置即可恢复）\n"
            f"{facts}（删除前取值）\n{safety.NO_RECHECK_NOTE}")


def _delete(path, project_root, data_dir=None):
    if safety.recyclable(path) and safety.recycle_delete(path):
        return "系统回收站"
    return safety.move_to_trash(path, project_root, data_dir=data_dir)


DELETE_PATH_SPEC = ToolSpec(name=NAME, description=DESCRIPTION, parameters=PARAMETERS,
                            level="C", run=run, plan=plan, cooperative=True,
                            effect="delete", resource_scope="path", retryable=False,
                            cancel_phase="before_commit", reconcile="backup_restore")
