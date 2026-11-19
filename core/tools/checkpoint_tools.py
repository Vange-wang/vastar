"""TASK-151①②：checkpoint 三工具（checkpoint_write / checkpoint_read / resume_status）。

- `checkpoint_write(path, fields)`：模型按题面落 checkpoint（如 `.work/n02/*.json`）
  一次调用即可——`task_id`／`status`／`updated_at` 由 harness 填（同源构造器
  `run_snapshot.build_snapshot`，与自动快照同一 schema，任务单 §一① 硬要求），
  模型只需带业务字段（如 `last_written`／`resume_hint`）。
- `checkpoint_read(path)`：读回并校验（缺失 / 坏 JSON / 缺字段 / hash 不符 → 显式报错）。
- `resume_status()`：返回本会话最新可信快照的「写到哪 / 下一步干什么」；
  无快照显式报错，**不许静默从头开始**（任务单 §一②）。
- 写入自守：路径必须在项目根内且不命中受保护路径；`target` 进审计（事件层留痕，
  与写操作组同键，供 record/judge 的 write_paths 审计）。
"""
from __future__ import annotations

import json
from pathlib import Path

from core.harness.run_snapshot import (
    CheckpointError,
    _body_hash,
    build_snapshot,
    latest_snapshot,
    read_snapshot_file,
    write_snapshot_file,
)
from core.tools.operation_receipt import ToolOutcome
from core.tools.registry import ToolSpec

CHECKPOINT_WRITE = "checkpoint_write"
CHECKPOINT_READ = "checkpoint_read"
RESUME_STATUS = "resume_status"
CHECKPOINT_TOOL_NAMES = {CHECKPOINT_WRITE, CHECKPOINT_READ, RESUME_STATUS}

_WRITE_PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "checkpoint 目标路径（相对项目根或绝对路径）"},
        "fields": {"type": "object",
                   "description": "业务字段（如 last_written/resume_hint/status）；"
                                  "task_id/updated_at 由框架自动填，无需手搓"},
    },
    "required": ["path", "fields"],
    "additionalProperties": False,
}
_READ_PARAMETERS = {
    "type": "object",
    "properties": {"path": {"type": "string", "description": "checkpoint 文件路径"}},
    "required": ["path"],
    "additionalProperties": False,
}
_RESUME_PARAMETERS = {"type": "object", "properties": {}, "additionalProperties": False}


def install_checkpoint_tools(context, raw: dict | None = None) -> None:
    """注册三工具并把自动快照器挂上 ToolsContext（round_trip 钩子取）。"""
    agent_cfg = (raw or {}).get("agent", {})
    from core.harness.run_snapshot import RunSnapshotter
    context.checkpoint_snapshots = RunSnapshotter(
        context.data_dir,
        every_n=int(agent_cfg.get("checkpoint_every_n", 1) or 1))
    context.registry.register(ToolSpec(
        CHECKPOINT_WRITE,
        "把任务 checkpoint 落到指定路径（一次调用）：task_id/status/updated_at 由框架自动填，"
        "fields 里只带业务字段（last_written/resume_hint 等）。字段集合与框架自动快照同源。",
        _WRITE_PARAMETERS, "C", lambda **_: "special handler required",
        effect="write", resource_scope="file", retryable=True, cooperative=True))
    context.registry.register(ToolSpec(
        CHECKPOINT_READ,
        "读取并校验一个 checkpoint 文件（缺字段/损坏/校验值不符会显式报错）。",
        _READ_PARAMETERS, "A", lambda **_: "special handler required",
        effect="read", resource_scope="file", retryable=True, cooperative=True))
    context.registry.register(ToolSpec(
        RESUME_STATUS,
        "返回本会话最新 run 快照：写到哪（last_written）、下一步干什么（resume_hint）。"
        "没有可信快照会显式报错，不会静默从头开始。",
        _RESUME_PARAMETERS, "A", lambda **_: "special handler required",
        effect="read", resource_scope="session", retryable=True, cooperative=True))


def _resolve_under_root(context, raw_path: str) -> Path:
    root = Path(context.project_root).resolve()
    p = Path(raw_path)
    resolved = (p if p.is_absolute() else root / p).resolve()
    if not (resolved == root or root in resolved.parents):
        raise CheckpointError(f"路径越出项目根：{raw_path}")
    for protected in context.protected_paths:
        if resolved == protected or protected in resolved.parents:
            raise CheckpointError(f"路径命中受保护目录：{raw_path}")
    return resolved


def _audit(path: Path) -> dict:
    return {"target": str(path), "location": "in_root",
            "permission": "ALLOW", "approval_source": "checkpoint_policy"}


def execute_checkpoint_tool(context, name, args, *, session_id: str,
                            request_id: str) -> ToolOutcome:
    """special_dispatch 入口（需要 session_id/run_id 的会话态工具）。"""
    snap = getattr(context, "checkpoint_snapshots", None)
    data_dir = snap.data_dir if snap is not None else context.data_dir
    try:
        if name == RESUME_STATUS:
            if data_dir is None:
                raise CheckpointError("data_dir 未初始化（legacy 布局），无快照可查")
            body = latest_snapshot(data_dir, session_id)
            return ToolOutcome(name=name, status="executed",
                               result="会话最新快照：\n" + json.dumps(
                                   body, ensure_ascii=False, indent=1),
                               args_summary="")
        path = _resolve_under_root(context, str(args.get("path") or ""))
        if name == CHECKPOINT_WRITE:
            fields = args.get("fields")
            if not isinstance(fields, dict):
                raise CheckpointError("fields 必须是对象（如 {\"last_written\": 42, "
                                      "\"resume_hint\": \"从 43 续写\"}）")
            body = build_snapshot(
                session_id=session_id, run_id=request_id,
                task_id=str(fields.get("task_id") or f"run:{request_id}"),
                status=str(fields.get("status") or "running"),
                last_written=fields.get("last_written"),
                resume_hint=str(fields.get("resume_hint")
                                or "未提供 resume_hint——建议补上续跑入口"))
            extra = {k: v for k, v in fields.items()
                     if k not in ("task_id", "status", "last_written", "resume_hint")}
            if extra:  # 业务扩展字段随快照保留（核心字段集合仍同源）
                body["extra_fields"] = extra
                body["sha256"] = _body_hash(body)
            write_snapshot_file(path, body)
            return ToolOutcome(name=name, status="executed",
                               result=f"checkpoint 已写入 {path}\n"
                                      + json.dumps(body, ensure_ascii=False, indent=1),
                               args_summary="", audit=_audit(path))
        body = read_snapshot_file(path)  # CHECKPOINT_READ
        return ToolOutcome(name=name, status="executed",
                           result=json.dumps(body, ensure_ascii=False, indent=1),
                           args_summary="")
    except CheckpointError as exc:
        # 显式报错（禁静默从头开始）：以 executed 返回结构化错误，模型可读可引述
        return ToolOutcome(name=name, status="executed",
                           result=json.dumps({"error": str(exc), "found": False},
                                             ensure_ascii=False),
                           args_summary="", reason=str(exc))
