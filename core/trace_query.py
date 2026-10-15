"""Vastar runtime component."""
from __future__ import annotations

import json

from core.tools.pagination import decode_cursor, encode_cursor
from memory.policy import sensitive

RETENTION_POLICY = ("events 只增不删（用户遗忘流程除外）；产物按 retention 列约束删除时机；"
                    "记忆修订只增不删——本视图只读，不改保留行为")

PAGE_SIZE_DEFAULT = 50
PAGE_SIZE_MAX = 200


def redacted(value, _key=None):
    """Vastar runtime component."""
    if _key is not None and sensitive(f"{_key}="):
        return "[已脱敏]"
    if isinstance(value, str):
        return "[已脱敏]" if sensitive(value) else value
    if isinstance(value, dict):
        return {k: redacted(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redacted(v) for v in value]
    return value


def _redact_error_json(raw):
    """Vastar runtime component."""
    try:
        return json.dumps(redacted(json.loads(raw)), ensure_ascii=False)
    except ValueError:
        return "[已脱敏]" if sensitive(raw) else raw


def _rows(store, sql, args=()):
    with store._lock:
        return [dict(r) for r in store._conn.execute(sql, args).fetchall()]


def _clamp_page_size(page_size: int) -> int:
    return max(1, min(int(page_size), PAGE_SIZE_MAX))


def trace_task(store, task_id: str) -> dict:
    """task → steps / runs / 操作计数 / 未落定操作（只读聚合；task 不存在 → ValueError）。"""
    task = store.task_get(task_id)   # 仓储口径：不存在即报错，禁静默重建
    runs = _rows(store, "SELECT run_id, state, terminal_state, finish_reason, retry_of_run_id,"
                        " step_id, created_at FROM runs WHERE task_id=? ORDER BY created_at",
                 (task_id,))
    return {"task": task, "runs": runs,
            "operation_counts": store.operation_state_counts(task_id),
            "operations_pending": store.operations_pending(task_id)}


def trace_run(store, run_id: str) -> dict:
    """run → attempts / model_calls / operations / 事件跨度 + 派生因果链（retry 父子）。"""
    rows = _rows(store, "SELECT run_id, session_id, task_id, step_id, state, terminal_state,"
                        " finish_reason, retry_of_run_id, created_at, updated_at"
                        " FROM runs WHERE run_id=?", (run_id,))
    if not rows:
        raise ValueError("Run 不存在；禁止静默重建")
    run = rows[0]
    
    
    attempts = []
    for a in _rows(store, "SELECT attempt_id, attempt_index, provider, model, started_at,"
                         " finished_at, usage_json, error_json FROM attempts WHERE run_id=?"
                         " ORDER BY attempt_index", (run_id,)):
        raw = a.pop("error_json")
        a = redacted(a)
        a["error_json"] = _redact_error_json(raw) if raw else raw
        attempts.append(a)
    calls = _rows(store, "SELECT call_id, record_json FROM history_model_calls WHERE run_id=?",
                  (run_id,))
    for c in calls:
        c["record"] = redacted(json.loads(c.pop("record_json")))
    children = _rows(store, "SELECT run_id, terminal_state FROM runs WHERE retry_of_run_id=?",
                     (run_id,))
    span = _rows(store, "SELECT COUNT(*) AS n, MIN(seq) AS first, MAX(seq) AS last"
                        " FROM events WHERE run_id=?", (run_id,))[0]
    operations = _rows(store, "SELECT operation_id, tool, state, task_id, step_id"
                              " FROM tool_operations WHERE run_id=?", (run_id,))
    return {"run": run, "attempts": attempts, "model_calls": calls, "operations": operations,
            "causation": {"parent_run": run["retry_of_run_id"], "child_runs": children,
                          "event_span": span,
                          "note": "信封无独立 causation 字段；链路由 retry_of_run_id + seq 派生"}}


def replay_run(store, run_id: str, *, cursor=None, page_size: int = PAGE_SIZE_DEFAULT) -> dict:
    """回放 = 从 events 表重建时间线视图（纯 SELECT，不执行工具、无副作用）。

    游标 = {"kind": "replay", "run_id", "after_seq"}；decode 失败或串 Run → 显式报错
    （pagination 口径：禁静默当首页）。"""
    after = 0
    if cursor:
        payload = decode_cursor(cursor)   # 非法游标 → ValueError（显式，不静默）
        if payload.get("kind") != "replay" or payload.get("run_id") != run_id:
            raise ValueError("游标不属于该 Run（显式失效，不静默当首页）")
        after = int(payload["after_seq"])
    events = store.events_page(run_id, after_seq=after, limit=_clamp_page_size(page_size) + 1,
                               inclusive=cursor is None)   
    page = events[:_clamp_page_size(page_size)]
    has_more = len(events) > len(page)
    next_cursor = (encode_cursor({"kind": "replay", "run_id": run_id,
                                  "after_seq": page[-1]["seq"]}) if has_more else None)
    return {"run_id": run_id, "has_more": has_more, "next_cursor": next_cursor,
            "events": [{"seq": e["seq"], "type": e["type"], "origin": e["origin"],
                        "timestamp_utc": e["timestamp_utc"], "payload": redacted(e["payload"])}
                       for e in page]}


def unfinished_tasks(store, project_id: str) -> list[dict]:
    """跨会话未完成清单（本项目）；FAILED/BLOCKED 照列——失败不剔除不美化（红线 4）。"""
    return store.tasks_open(project_id)


def artifacts_list(store, owner: str, *, cursor=None, page_size: int = PAGE_SIZE_DEFAULT) -> dict:
    """产物列表分页（rowid 键集次序：新增不挤动已翻页）；游标越 owner/类型 → 显式失效。"""
    after = 0
    if cursor:
        payload = decode_cursor(cursor)
        if payload.get("kind") != "artifacts" or payload.get("owner") != owner:
            raise ValueError("游标越 owner 或类型不符（显式失效，不静默当首页）")
        after = int(payload["after"])
    rows = store.artifacts_page(owner=owner, after=after, limit=_clamp_page_size(page_size) + 1)
    page = rows[:_clamp_page_size(page_size)]
    has_more = len(rows) > len(page)
    next_cursor = (encode_cursor({"kind": "artifacts", "owner": owner,
                                  "after": page[-1]["rowid"]}) if has_more else None)
    for r in page:
        r.pop("rowid", None)
    return {"items": page, "has_more": has_more, "next_cursor": next_cursor}
