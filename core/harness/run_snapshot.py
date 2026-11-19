"""TASK-151①②：框架级 run 快照（自动 checkpoint）与同源读写 / 续跑原语。

设计要点（对应任务单 §一①②）：
- **自动快照不靠模型自觉**：每次工具执行后由 round_trip 钩子调 `on_tool_executed`
  （`every_n` 可配，`agent.checkpoint_every_n`，默认 1），快照写产品自有位置
  `<data_dir>/runs/<run_id>/checkpoint.json`。
- **同源 schema**：`SNAPSHOT_FIELDS` 是唯一字段来源——自动快照与 `checkpoint_write`
  工具产出的字段集合都经 `build_snapshot` 构造，不许两套 schema。
- **续跑原语**：`latest_snapshot` 供 `resume_status` 工具与启动提示恢复
  「写到哪 / 下一步干什么」；快照缺失或不可信（坏 JSON / 缺字段 / hash 不符）
  一律抛 `CheckpointError`——**不许静默从头开始**。
- `last_written` 粘性：只有携带写入目标（`target`）的工具事件才更新它，
  只读事件保留上一次的写入位置。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

SNAPSHOT_VERSION = 1
# 单一字段来源：自动快照与 checkpoint_write 工具共用（任务单 §一①「同源」硬要求）
SNAPSHOT_FIELDS = ("task_id", "status", "last_written", "resume_hint", "updated_at")

WRITE_EFFECT_TOOLS_HINT = "（最近一次写入：{target}，第 {n} 次工具执行）"


class CheckpointError(Exception):
    """快照缺失 / 不可信 / 参数非法——显式报错，不静默降级。"""


def snapshot_path(data_dir: Path, run_id: str) -> Path:
    return Path(data_dir) / "runs" / run_id / "checkpoint.json"


def _body_hash(body: dict) -> str:
    payload = {k: body[k] for k in ("snapshot_version", "session_id", "run_id",
                                    *SNAPSHOT_FIELDS)}
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def build_snapshot(*, session_id: str, run_id: str, task_id: str, status: str,
                   last_written, resume_hint: str, updated_at: str | None = None) -> dict:
    """同源构造器：框架自动快照与 checkpoint_write 工具都走这里。"""
    body = {
        "snapshot_version": SNAPSHOT_VERSION,
        "session_id": session_id,
        "run_id": run_id,
        "task_id": task_id,
        "status": status,
        "last_written": last_written,
        "resume_hint": resume_hint,
        "updated_at": updated_at or time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    body["sha256"] = _body_hash(body)
    return body


def write_snapshot_file(path: Path, body: dict) -> Path:
    """原子落盘（tmp + os.replace），写后回读校验（不一致显式报错，禁静默）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(body, ensure_ascii=False, indent=1)
    tmp = path.with_suffix(".json.tmp")
    data = text.encode("utf-8")
    tmp.write_bytes(data)  # bytes 写入：避免 Windows 文本模式 \n→\r\n 转换破坏自校验
    os.replace(tmp, path)
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    expected = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise CheckpointError(f"快照写后校验不一致（{path}）")
    return path


def read_snapshot_file(path: Path) -> dict:
    """读取并校验快照：缺失 / 坏 JSON / 缺字段 / hash 不符 → CheckpointError。"""
    path = Path(path)
    if not path.exists():
        raise CheckpointError(f"快照缺失：{path} 不存在")
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise CheckpointError(f"快照不可信：{path} 不是合法 JSON（{exc}）") from exc
    if not isinstance(body, dict):
        raise CheckpointError(f"快照不可信：{path} 顶层不是对象")
    missing = [f for f in SNAPSHOT_FIELDS if f not in body]
    if missing:
        raise CheckpointError(f"快照不可信：{path} 缺字段 {missing}")
    if body.get("sha256") != _body_hash(body):
        raise CheckpointError(f"快照不可信：{path} 校验值不符（可能被手工篡改或半截写入）")
    return body


class RunSnapshotter:
    """每次工具执行后的自动快照器（round_trip 钩子调用；N 可配）。"""

    def __init__(self, data_dir: Path | None, every_n: int = 1) -> None:
        self._data_dir = Path(data_dir) if data_dir else None
        self._every_n = max(1, int(every_n or 1))

    @property
    def data_dir(self) -> Path | None:
        return self._data_dir

    def on_tool_executed(self, *, session_id: str, run_id: str, task_id: str | None,
                         tool: str, target: str | None, exec_count: int) -> Path | None:
        if self._data_dir is None:
            raise CheckpointError("data_dir 未初始化（legacy 布局），无法写 run 快照")
        if exec_count % self._every_n:
            return None
        path = snapshot_path(self._data_dir, run_id)
        last_written = None
        if path.exists():  # 粘性：只读事件保留上一次写入位置
            try:
                last_written = read_snapshot_file(path)["last_written"]
            except CheckpointError:
                last_written = None
        if target:
            last_written = {"tool": tool, "target": target, "exec_count": exec_count}
        hint = f"自动快照：run {run_id} 进行中；最近执行 {tool}（第 {exec_count} 次工具执行）"
        if target:
            hint += f"，写入 {target}"
        hint += "。中断重启后用 resume_status 核对续跑点。"
        body = build_snapshot(
            session_id=session_id, run_id=run_id,
            task_id=task_id or f"run:{run_id}", status="running",
            last_written=last_written, resume_hint=hint)
        return write_snapshot_file(path, body)


def latest_snapshot(data_dir: Path, session_id: str) -> dict:
    """找该会话最新的可信快照；没有或全部不可信 → CheckpointError（显式，不静默）。"""
    root = Path(data_dir) / "runs"
    if not root.exists():
        raise CheckpointError(f"无可用快照：{root} 不存在（本会话没有任何 run 快照）")
    best = None
    errors = []
    for path in root.glob("*/checkpoint.json"):
        try:
            body = read_snapshot_file(path)
        except CheckpointError as exc:
            errors.append(str(exc))
            continue
        if body.get("session_id") == session_id:
            if best is None or str(body["updated_at"]) > str(best["updated_at"]):
                best = body
    if best is None:
        detail = f"；另有 {len(errors)} 个损坏快照" if errors else ""
        raise CheckpointError(f"无可用快照：会话 {session_id} 没有可信的 run 快照{detail}")
    return best
