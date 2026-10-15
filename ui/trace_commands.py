"""/trace 命令（B40.02）：追踪关联与回放的真人可读视图。

只读（红线 2/3）：本模块只是把 core/trace_query 的查询结果排成人话——不执行工具、
不产生副作用、不触碰授权。术语降噪：一律用「任务/步骤/运行/模型调用/动作/产物」，
内部代号只出现在括号备注里。
"""
import json

from core import trace_query
from ui.usage_view import call_usage, operation_state

HELP = ("/trace task [任务ID] 任务链（默认当前任务） · /trace run [--json] <运行ID> 运行链 · "
        "/trace replay <运行ID> [游标] 事件回放（只重建视图，不执行工具） · "
        "/trace unfinished 未完成清单 · /trace ops [任务ID] 未落定动作 · "
        "/trace artifacts [游标] 产物列表。")


def _clip(text: str, n: int = 80) -> str:
    text = str(text)
    return text if len(text) <= n else text[:n] + "…"


def _fmt_task_view(d: dict) -> str:
    task = d["task"]
    lines = [f"任务 {task['task_id']} · 状态 {task['status']} · 修订 r{task['revision']}",
             f"目标：{_clip(task['goal'])}"]
    for s in task["steps"]:
        lines.append(f"  步骤 {s['step_id']} [{s['status']}] {_clip(s['goal'], 60)}")
    if task["remaining_work"]:
        lines.append(f"未完成步骤：{', '.join(task['remaining_work'])}")
    counts = d["operation_counts"]
    if counts:
        lines.append("动作计数：" + " · ".join(f"{operation_state(k)} {v}" for k, v in sorted(counts.items())))
    for op in d["operations_pending"]:
        lines.append(f"  待落定 {op['operation_id']} · {op['tool']} · {operation_state(op['state'])}")
    for r in d["runs"]:
        retry = f" · 重试自 {r['retry_of_run_id']}" if r["retry_of_run_id"] else ""
        lines.append(f"  运行 {r['run_id']} [{r['terminal_state'] or r['state']}]{retry}")
    return "\n".join(lines)


def _failure_summary(d: dict) -> str | None:
    """Vastar runtime component."""
    run = d["run"]
    if (run["terminal_state"] or run["state"]) != "FAILED":
        return None
    reason = "原因未记录"
    for a in reversed(d["attempts"]):
        raw = a.get("error_json")
        if raw:
            try:
                err = json.loads(raw)
                reason = f"{err.get('code', '未知错误')}（{err.get('message', '无详情')}）"
            except (ValueError, AttributeError):
                reason = str(raw)
            break
    return (f"失败原因：{reason} · 共尝试 {len(d['attempts'])} 次；"
            f"下一步：/retry 重问同一输入，或 /trace replay {run['run_id']} 回放事件看细节")


def _fmt_run_view(d: dict) -> str:
    """Vastar runtime component."""
    run = d["run"]
    lines = [f"运行 {run['run_id']} · 状态 {run['terminal_state'] or run['state']}"
             f" · 结束原因 {run['finish_reason'] or '未记录'}"]
    failure = _failure_summary(d)
    if failure:
        lines.append(failure)
    lines.append(f"  会话 {run['session_id']} · 任务 {run['task_id'] or '未关联'}"
                 f" · 步骤 {run['step_id'] or '未关联'}")
    for a in d["attempts"]:
        lines.append(f"  尝试 #{a['attempt_index']} {a['provider']}/{a['model']}"
                     f" · {a['started_at']} → {a['finished_at'] or '未完'}")
    for c in d["model_calls"]:
        rec = c["record"]
        lines.append(f"  模型调用 {c['call_id']} · 用量 {call_usage(rec)}")
    for op in d["operations"]:
        lines.append(f"  动作 {op['operation_id']} · {op['tool']} · {operation_state(op['state'])}")
    return "\n".join(lines)


def _fmt_replay(d: dict) -> str:
    lines = [f"回放 {d['run_id']}（只重建视图，未执行任何工具）"]
    for e in d["events"]:
        lines.append(f"  #{e['seq']} {e['type']} · {e['origin']} · "
                     f"{_clip(json.dumps(e['payload'], ensure_ascii=False), 100)}")
    if not d["events"]:
        lines.append("  （该 Run 无事件记录）")
    if d["has_more"]:
        lines.append(f"还有后续事件：/trace replay {d['run_id']} {d['next_cursor']}")
    return "\n".join(lines)


def _fmt_unfinished(rows: list) -> str:
    if not rows:
        return "未完成清单：无（本项目没有未闭合任务）"
    lines = ["未完成清单（失败/阻塞照列，不剔除）："]
    for r in rows:
        lines.append(f"  {r['task_id']} [{r['status']}] 剩余步骤 {r['remaining']}"
                     f" · {_clip(r['goal'], 60)}")
    return "\n".join(lines)


def _fmt_ops(rows: list) -> str:
    if not rows:
        return "未落定动作：无"
    lines = ["未落定动作（已提交前取消不算失败；待核实需 /task reconcile 核实）："]
    for op in rows:
        lines.append(f"  {op['operation_id']} · {op['tool']} · {operation_state(op['state'])}"
                     f" · 任务 {op['task_id'] or '未关联任务（本次为独立动作）'} · 更新于 {op['updated_at']}")
    return "\n".join(lines)


def _fmt_artifacts(page: dict) -> str:
    items = page["items"]
    if not items:
        return "产物列表：空（本项目无在册产物）"
    lines = ["产物列表（读取用 read_artifact 工具或引用 uri）："]
    for a in items:
        lines.append(f"  {a['uri']} · {a['size']}B · {a['media_type']} · 保留 {a['retention']}"
                     f" · {_clip(a['created_at'], 19)}")
    if page["has_more"]:
        lines.append(f"还有后续：/trace artifacts {page['next_cursor']}")
    return "\n".join(lines)


def trace_command(coord, arg: str) -> str:
    store = getattr(coord, "_store", None)
    service = getattr(coord, "task_service", None)
    if store is None or service is None:
        raise ValueError("追踪视图未装配（任务服务/存储缺失）")
    cmd, _, tail = arg.partition(" ")
    tail = tail.strip()
    if cmd in ("", "help"):
        return HELP + " 保留策略：" + trace_query.RETENTION_POLICY
    if cmd == "task":
        task_id = tail or service.current(coord.session_id)["task_id"]
        return _fmt_task_view(trace_query.trace_task(store, task_id))
    if cmd == "run":
        as_json = "--json" in tail.split()
        run_id = tail.replace("--json", "").strip()
        if not run_id:
            raise ValueError("用法：/trace run [--json] <运行ID>")
        view = trace_query.trace_run(store, run_id)
        return (json.dumps(view, ensure_ascii=False, indent=2) if as_json
                else _fmt_run_view(view))
    if cmd == "replay":
        run_id, _, cursor = tail.partition(" ")
        if not run_id:
            raise ValueError("用法：/trace replay <运行ID> [游标]")
        return _fmt_replay(trace_query.replay_run(store, run_id, cursor=cursor or None))
    if cmd == "unfinished":
        return _fmt_unfinished(trace_query.unfinished_tasks(store, service.project_id))
    if cmd == "ops":
        return _fmt_ops(store.operations_pending(tail or None))
    if cmd == "artifacts":
        return _fmt_artifacts(trace_query.artifacts_list(store, service.project_id,
                                                         cursor=tail or None))
    raise ValueError(HELP)


def handle_trace(ui, arg: str) -> None:
    try:
        ui._renderer.render_note(trace_command(ui._coordinator, arg))
    except (ValueError, KeyError, TypeError) as exc:
        ui._renderer.render_note(f"追踪查询不可用：{exc}")
