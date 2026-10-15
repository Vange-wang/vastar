"""写工具计划、权限与备份流程，独立于线程执行和取消控制。"""
import asyncio
import time
from pathlib import Path

from core.tools import impact_scan, permissions, safety
from core.tools.executor import (
    ToolOutcome,
    _ask_user,
    _build_question,
    _denied_outcome,
    _EarlyPlan,
)
from core.tools.operation_runner import thread_call
from core.tools.registry import ToolParamError, ToolSpec


async def execute_planned(executor, spec: ToolSpec, args: dict, resolved: Path,
                           resolved_extra: dict, cancel_event: asyncio.Event,
                           started: int, args_sum: str, ask_emitter, *,
                           session_id: str = "", request_id: str = "", control=None) -> ToolOutcome:
    # 已解析路径先行否决（plan 之前）：主路径与附加路径（dest）上**不存在**的
    
    for early_target in [resolved, *resolved_extra.values()]:
        early_deny = permissions.hard_deny_reason(
            early_target, executor._ctx.project_root, executor._ctx.protected_paths)
        if early_deny is not None:
            return _denied_outcome(spec.name, early_deny, "deny_rule", args_sum,
                                _EarlyPlan(early_target), started)
    try:
        control.checkpoint("prepare")
        plan = await thread_call(spec.plan, args, resolved_path=resolved,
                                       resolved_extra=resolved_extra)
    except ToolParamError as exc:
        return ToolOutcome(name=spec.name, status="failed", reason=str(exc),
                           duration_ms=(time.monotonic_ns() - started) // 1_000_000,
                           args_summary=args_sum, origin="tool_param")
    control.checkpoint("after_plan")
    all_targets = [resolved, *plan.targets]
    denied, toctou_fp, impact = _post_plan_guard(
        spec, all_targets, resolved, plan, executor._ctx.project_root, args_sum, started,
        executor._ctx.protected_paths)
    if denied is not None:
        return denied
    if (plan.needs_recycle
            and permissions.location_of(resolved, executor._ctx.project_root)
            == permissions.OUT_ROOT and not safety.recyclable(resolved)):
        return _denied_outcome(
            spec.name,
            "项目外删除需要系统回收站兜底，当前盘不支持回收站——已拒绝（禁硬删，QD-117）",
            "deny_rule", args_sum, plan, started)
    decision = permissions.decide_operation(
        action=plan.action, targets=all_targets, project_root=executor._ctx.project_root,
        count=plan.target_count, total_bytes=plan.total_bytes, recursive=plan.recursive)
    prepared = await _backup_and_audit(executor, spec, plan, decision, resolved,
                                       args_sum, started, control, impact)
    if isinstance(prepared, ToolOutcome):
        return prepared
    backup_path, audit_fields = prepared
    approved = await authorize(executor, spec, plan, decision, resolved, backup_path,
                               args_sum, started, ask_emitter, cancel_event, session_id, request_id,
                               resolved_extra)
    if isinstance(approved, ToolOutcome):
        if approved.audit.get("approval_source") == "user_cancel":
            control.transition("cancelled_before_commit")
        control.facts.update(approved.audit)
        return approved
    control.checkpoint("after_authorization")
    verdict, source = approved
    gate = executor._ctx.ask_gate
    if gate is not None:
        epoch, policy = gate.authorization_epoch, gate.policy_revision
        control.authorization_check = lambda: (gate.authorization_epoch == epoch and gate.policy_revision == policy)
        control.facts["authorization_epoch"] = epoch
    audit_fields["permission"] = verdict
    audit_fields["approval_source"] = source
    denied = _toctou_guard(spec, toctou_fp, args_sum, plan, started)
    if denied is not None:
        return denied
    return await executor._run_with_race(spec, args, resolved, cancel_event, started,
                                     args_sum, resolved_extra=resolved_extra,
                                     audit_fields=audit_fields, control=control)


async def _backup_and_audit(executor, spec, plan, decision, resolved, args_sum,
                            started, control, impact):
    verdict = decision.verdict
    backup_path = None
    if verdict != permissions.VERDICT_DENY and plan.backup_source is not None:
        try:
            control.checkpoint("before_backup")
            backup_path = await thread_call(safety.backup_file,
                                                  plan.backup_source, executor._ctx.project_root,
                                                  **({"data_dir": executor._ctx.data_dir}
                                                     if executor._ctx.data_dir else {}))
        except OSError as exc:
            return _denied_outcome(
                spec.name, f"覆盖前备份失败（{exc}）——底线：备份失败必须拒绝写入",
                "backup_failed", args_sum, plan, started, origin="backup_failed")
    audit_fields = {"action": plan.action, "target": str(resolved),
                    "location": decision.location_category, "batch": decision.batch,
                    "count": plan.target_count, "bytes": plan.total_bytes,
                    "overwrite": plan.overwrite}
    if impact is not None:
        audit_fields["impact_scan"] = impact
    if backup_path is not None:
        audit_fields["backup_path"] = str(backup_path)
    control.facts.update(audit_fields)
    control.checkpoint("after_backup")
    return backup_path, audit_fields


def _post_plan_guard(spec, all_targets, resolved, plan, project_root, args_sum, started,
                     protected_paths=()):
    """Vastar runtime component."""
    for target in all_targets:   # 受影响全集任一命中（如目录内嵌硬拒项）→ 整批 DENY
        deny = permissions.hard_deny_reason(target, project_root, protected_paths)
        if deny is not None:
            return _denied_outcome(spec.name, deny, "deny_rule", args_sum, plan, started), None, None
    # B10 TOCTOU：plan 时点指纹快照（授权/备份可能耗时数十秒，窗口必须显式收口）
    toctou_fp = impact_scan.fingerprint(all_targets)
    impact = None
    if plan.recursive:
        scan = impact_scan.scan_tree(resolved, project_root)
        impact = {"entries": scan.entries, "links": scan.links,
                  "sensitive": scan.sensitive, "case_conflicts": scan.case_conflicts}
    return None, toctou_fp, impact


def _toctou_guard(spec, toctou_fp, args_sum, plan, started):
    """B10 TOCTOU：执行前复核指纹——检查后目标被替换/修改 → 显式拒绝（禁按旧事实执行）。"""
    changed = impact_scan.recheck(toctou_fp)
    if not changed:
        return None
    shown = "；".join(changed[:3]) + ("；…" if len(changed) > 3 else "")
    return _denied_outcome(
        spec.name,
        f"检查后目标已变化（TOCTOU 复核检出 {len(changed)} 项：{shown}）——已显式拒绝，请重新发起",
        "toctou_recheck", args_sum, plan, started, origin="toctou_recheck")


async def authorize(executor, spec, plan, decision, resolved, backup_path,
                    args_sum, started, ask_emitter, cancel_event, session_id, request_id,
                    resolved_extra):
    from core.skills.workflow_guard import authorize_preview, is_organization
    if is_organization(executor._ctx.skills) and decision.verdict != permissions.VERDICT_DENY:
        return await authorize_preview(executor, spec, plan, decision, resolved, resolved_extra,
                                       backup_path, args_sum, started, ask_emitter,
                                       cancel_event, session_id, request_id)
    verdict = decision.verdict
    source = "allow_rule" if verdict == permissions.VERDICT_ALLOW else ""
    if verdict == permissions.VERDICT_ASK:
        if executor._ctx.permission_mode == permissions.PERMISSION_AUTO:
            verdict, source = permissions.VERDICT_ALLOW, "auto"   # 放开档：免问，硬拒已先行
        else:
            gate = executor._ctx.ask_gate
            if gate is None:
                return _denied_outcome(spec.name,
                                    "需要确认但询问通道未装配——按拒绝处理（禁静默放行）",
                                    "ask_unavailable", args_sum, plan, started, origin="ask_gate")
            question = _build_question(spec.name, plan, decision, resolved, backup_path)
            if ask_emitter is not None:
                await ask_emitter({"tool": spec.name, "lines": question.lines,
                                   "action_category": question.action_category,
                                   "location_category": question.location_category,
                                   "source": "model"})
            answer = await _ask_user(gate, question, session_id=session_id,
                                     request_id=request_id,
                                     project_root=executor._ctx.project_root,
                                     cancel_event=cancel_event)
            if not answer.approved:
                label = gate.deny_label(answer.approval_source)
                return _denied_outcome(spec.name, f"需要确认的操作未获允许：{label}",
                                    answer.approval_source, args_sum, plan, started,
                                    origin="ask_gate")
            verdict, source = permissions.VERDICT_ALLOW, answer.approval_source
    return verdict, source
