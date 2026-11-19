"""/status 组装（B40 职责拆分自 ui/repl.py 移出；B40.01/03 扩展展示面）。

UI 只消费状态（TASK-2026-10-05-77 红线 2）：本模块全部只读——不触碰授权、
不触发执行；数据一律来自协调器与仓储的既有口径。**注意**：QuestionService.pending
与 TaskRuntime.snapshot 内部有写入（过期 UPDATE / INSERT OR IGNORE），/status 不调
它们，改走只读直读（见 _waiting_count / _task_snapshot_row）。
术语降噪（B40.03）：一律中文人话标签，内部代号只出现在括号备注里。
"""
import time

from core.run_progress import progress_lines
from ui.usage_view import run_usage


def _last_finish_note(last) -> str:
    """/status 的上一轮 finish 文案（TASK-34/QD-276 第二落点：与结束行同口径）。"""
    if last.status == "incomplete" and last.finish_reason == "output_limit":
        return "已达单次输出上限，可 /continue 续写"
    if last.status == "incomplete" and last.finish_reason == "run_total_timeout":
        return "已达单次时长上限，可 /continue 续写"
    return last.finish_reason


def _status_lines(coord, registry, permission_mode: str | None, *,
                  startup_facts: str = "") -> list[str]:
    """组装 /status 文本（模块级——V6 类红线；TASK-24 增权限模式展示）。

    含：会话/模式/队列/存储 + 模型/输出上限/忙碌/上一轮 + 工具启用数/最近调用/权限模式 +
    本轮模型调用与工具调用次数（成本可观测，Spec §6）。
    TASK-116 §2：启动预检段不再显示在启动页，改由本函数挂载（startup_facts 为启动时
    已计算的快照文本，/status 不重跑预检、不产生副作用）；Output 诊断同挪入。
    """
    last = coord.last_result
    lines = [
        f"会话 {coord.session_id} · 模式 {coord.mode} · 队列 {coord.has_draft_queue()} 条"
        f" · 存储 {coord.storage_health_detail()}",
        f"模型 {coord.primary_label()} · 输出上限 {coord.output_limit_label()}"
        f" · 忙碌 {coord.is_busy()}"
        + (f" · 上一轮 {last.status}（{_last_finish_note(last)}）" if last else ""),
    ]
    lines.extend(progress_lines(coord))
    if registry is None or not registry.enabled:
        lines.append("工具 未启用（default-closed）")
    else:
        recent = registry.last_call or {}
        suffix = (f" · 最近 {recent['tool']}/{recent['status']}"
                  f"/{recent['duration_ms']}ms" if recent else "")
        mode = f" · 权限模式 {permission_mode}" if permission_mode else ""
        lines.append(f"工具 启用 {len(registry.enabled)} 项{suffix}{mode}")
    if last is not None:
        label = '上一轮' if coord.is_busy() else '本轮'
        lines.append(f"{label}模型调用 {last.model_calls} 次"
                     f" · 工具调用 {last.tool_executions} 次"
                     "（已结束运行的统计）")
    for section in (_section_task, _section_usage, _section_memory, _section_compact,
                    _section_artifacts, _section_processes, _section_next_step):
        lines.extend(section(coord))
    if startup_facts:
        lines.append("启动预检（启动页不再显示，信息保留在此）：")
        lines.extend(f"  {line}" for line in startup_facts.splitlines())
    from ui.renderer import active_output_class  # 延迟导入：避免与 renderer 循环
    lines.append(f"输出实现：{active_output_class()}")
    return lines


def _task_snapshot_row(store, task_id: str) -> dict | None:
    """task_runtime 只读快照（不调 TaskRuntime.snapshot——它 INSERT OR IGNORE，/status 不写库）。"""
    with store._lock:
        row = store._conn.execute("SELECT * FROM task_runtime WHERE task_id=?",
                                  (task_id,)).fetchone()
    return dict(row) if row else None


def _waiting_count(store, task_id: str) -> int:
    """待回答问题计数（只读口径：不过期落库，只按 expires_at 过滤——过期写是命令侧职责）。"""
    with store._lock:
        return store._conn.execute(
            "SELECT COUNT(*) FROM task_questions WHERE task_id=? AND state='pending'"
            " AND expires_at>?", (task_id, time.time())).fetchone()[0]


def _section_task(coord) -> list[str]:
    """任务/步骤 + 等待 + 已提交/待核实动作 + 恢复异常（服务未装配或无任务时降级）。"""
    service = getattr(coord, "task_service", None)
    store = getattr(coord, "_store", None)
    if service is None or store is None:
        return []
    try:
        task = service.current(coord.session_id)
    except ValueError:
        return ["任务：无（/task help 了解结构化任务）"]
    steps = task["steps"]
    done = sum(1 for s in steps if s["status"] == "COMPLETED")
    line = f"任务 [{task['status']}] 步骤 {done}/{len(steps)} 完成"
    if task["remaining_work"]:
        line += " · 剩余 " + ", ".join(task["remaining_work"][:3])
    lines = [line + "（/task status 详情）"]
    waiting = _waiting_count(store, task["task_id"])
    if waiting or task["status"] == "WAITING_INPUT":
        hint = f"等待：{waiting} 个问题待回答（/task answer）"
        if task["status"] == "WAITING_INPUT":
            hint += "；任务正等待输入"
        lines.append(hint)
    counts = store.operation_state_counts(task["task_id"])
    if counts:
        label = {"committed": "已提交", "unknown": "待核实", "executing": "执行中",
                 "prepared": "已登记", "failed": "失败", "not_committed": "未提交"}
        lines.append("动作：" + " · ".join(f"{label.get(k, k)} {v}" for k, v in sorted(counts.items())))
    snap = _task_snapshot_row(store, task["task_id"])
    if snap and snap.get("recovery_error"):
        # HM-157（TASK-82）：无检查点时 /task resume 必败（缺已提交检查点）——按实际可续
        # 条件给提示，与 _section_next_step 的 HM-150 出路口径一致，不混入必败动作。
        if store.checkpoint_latest(task["task_id"]) is None:
            lines.append(f"恢复异常：{snap['recovery_error']}（无检查点，不可恢复；"
                         "/new 开新会话）")
        else:
            lines.append(f"恢复异常：{snap['recovery_error']}（/task resume 恢复）")
    return lines


def _section_usage(coord) -> list[str]:
    """用量：估算 vs 实际（actual 未到达照实说；费用只在全量报到时给数）。"""
    last = coord.last_result
    if last is None:
        return []
    budget = getattr(last, "budget_summary", None) or {}
    totals = getattr(last, "usage_totals", None) or {}
    cost = getattr(last, "cost_summary", None) or {}
    parts = []
    if budget.get("estimated_tokens") is not None:
        parts.append(f"估算 ~{budget['estimated_tokens']} tokens")
    usage_text = run_usage(totals, getattr(last, 'call_records', []), getattr(last, 'model_calls', 0))
    if usage_text:
        parts.append("实际 " + usage_text if usage_text.startswith("入") else usage_text)
    if cost.get("status") == "calculated_from_reported_usage":
        parts.append(f"费用 {cost['amount']} {cost['currency']}")
    return ["用量：" + " · ".join(parts)] if parts else []


def _section_memory(coord) -> list[str]:
    """记忆条目数与版本对照（冻结 vs 最新；来源明细走 /memory list）。"""
    service = getattr(coord, "memory_service", None)
    if service is None:
        return []
    try:
        info = service.list(coord.session_id)
    except (ValueError, KeyError, TypeError, AttributeError):
        return []
    newer = "有更新未生效" if info.get("newer_than_session") else "与冻结一致"
    return [f"记忆：{len(info.get('items', []))} 条 · 版本 r{info.get('memory_revision')}"
            f" · 本会话冻结 r{info.get('session_memory_revision')}（{newer}）· 来源 /memory list"]


def _section_compact(coord) -> list[str]:
    """compact 状态：生效快照 / 待审批快照（含可行动提示）。"""
    store = getattr(coord, "_store", None)
    if store is None:
        return []
    pending = store.latest_compact_snapshot(coord.session_id, status="pending")
    approved = store.latest_compact_snapshot(coord.session_id, status="approved")
    if not pending and not approved:
        return ["压缩：无快照（/compact 生成）"]
    parts = []
    if approved:
        parts.append(f"生效 {approved.get('snapshot_id', '?')}")
    if pending:
        parts.append(f"待审批 {pending.get('snapshot_id', '?')}（/compact ok 批准 / drop 丢弃）")
    return ["压缩：" + " · ".join(parts)]


def _section_artifacts(coord) -> list[str]:
    """artifact 入口（在册计数 + 查询指引；无产物不刷屏）。"""
    store = getattr(coord, "_store", None)
    service = getattr(coord, "task_service", None)
    ctx = getattr(coord, "task_tools_context", None)
    if store is None or service is None or getattr(ctx, "artifacts", None) is None:
        return []
    count = store.artifacts_count(owner=service.project_id)
    if not count:
        return []
    return [f"产物：{count} 件在册（/trace artifacts 列表；read_artifact 按引用续读）"]


def _section_processes(coord) -> list[str]:
    """进程/浏览器状态（只读计数；服务未装配则不显示该行）。"""
    ctx = getattr(coord, "task_tools_context", None)
    if ctx is None:
        return []
    parts = []
    ps = getattr(ctx, "process_service", None)
    if ps is not None:
        entries = getattr(ps, "entries", None) or {}
        active = sum(1 for e in entries.values()
                     if (e.get("info") or {}).get("status") in ("preparing", "started"))
        parts.append(f"进程 在跑 {active}/共 {len(entries)}")
    bs = getattr(ctx, "browser_service", None)
    if bs is not None:
        parts.append(f"浏览器 会话 {len(getattr(bs, 'sessions', None) or {})}")
    return ["外部资源：" + " · ".join(parts)] if parts else []


def _section_next_step(coord) -> list[str]:
    """错误后下一步（可行动提示；失败照实提示，不美化）。"""
    hints = []
    last = coord.last_result
    if last is not None:
        # HM-157（TASK-82）：提示与实际可续条件一致（continue_last：incomplete ∧ partial）——
        # needs_input 等 partial=False 的未完成态下 /continue 必败，不混入提示。
        if last.status == "incomplete" and getattr(last, "partial", False):
            hints.append("/continue 续写")
        elif last.status == "failed":
            # HM-142（TASK-78）：提示必须可操作——直接带最近运行 ID，无需事先知道
            run_id = getattr(last, "run_id", None)
            hints.append(f"/retry 重问 · /trace run {run_id} 定位失败" if run_id
                         else "/retry 重问")
    service = getattr(coord, "task_service", None)
    store = getattr(coord, "_store", None)
    if service is not None and store is not None:
        try:
            task = service.current(coord.session_id)
        except ValueError:
            task = None
        if task:
            snap = _task_snapshot_row(store, task["task_id"])
            if snap and snap.get("recovery_error"):
                # HM-150：无检查点时 /task resume 必败——提示必须给出路，不把用户卡死
                if store.checkpoint_latest(task["task_id"]) is None:
                    hints.append("现场无检查点、不可恢复：/new 开新会话"
                                 "（任务留在原会话，可 /task attach 回挂）")
                else:
                    hints.append("/task resume 恢复中断任务")
    return ["下一步：" + "；".join(hints)] if hints else []
