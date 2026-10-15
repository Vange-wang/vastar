"""Vastar runtime component."""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path

from core.tools import audit, permissions
from core.tools.ask_gate import AskAnswer, AskGate, AskQuestion, build_auth
from core.tools.operation_receipt import MAX_RESULT_BYTES, ToolOutcome, run_sync

__all__ = ["MAX_RESULT_BYTES", "ToolOutcome", "ToolExecutor"]
from core.tools.registry import ToolsContext, ToolSpec

_EXTRA_PATH_KEYS = ("dest",)   # 多路径参数工具（move_file）的附加路径键


class _EarlyPlan:
    """plan 之前硬拒否决的占位（真实 plan 未产出；_denied 只消费动作与数量字段）。"""

    def __init__(self, target: Path) -> None:
        self.action = "early_deny"
        self.target_count = 1
        self.overwrite = False


class ToolExecutor:
    """执行层闸门：先 permissions 判定，通过才调用工具实现（Spec §5 依赖方向）。"""

    def __init__(self, context: ToolsContext) -> None:
        self._ctx = context

    async def execute(self, name: str, args: dict, *,
                      cancel_event: asyncio.Event, ask_emitter=None,
                      session_id: str = "", request_id: str = "", operation_id: str | None = None) -> ToolOutcome:
        """执行一个工具调用。B08：session_id/request_id 透传给询问闸门——临时授权
        绑定会话与请求（scope 判定与存储在 gate，本层不持有权限表）。"""
        started = time.monotonic_ns()
        registry = self._ctx.registry
        args_sum = audit.args_summary(args)

        spec = registry.get(name)
        if spec is None:
            return ToolOutcome(name=name, status="rejected",
                               reason="工具未注册（default-closed）", args_summary=args_sum,
                               origin="registry_gate")
        if not registry.is_enabled(name):
            return ToolOutcome(name=name, status="rejected",
                               reason="工具已注册但未启用（default-closed）", args_summary=args_sum,
                               origin="registry_gate")
        from core.tools.special_dispatch import dispatch
        special = await dispatch(self._ctx, spec, args, args_summary=args_sum, session_id=session_id,
                                  request_id=request_id, operation_id=operation_id,
                                  cancel_event=cancel_event, ask_emitter=ask_emitter)
        if special is not None:
            return special
        from core.skills.workflow_guard import attach_receipt, is_organization
        organization = is_organization(self._ctx.skills) and spec.effect in ("move", "delete")
        decision = permissions.check_level(spec.level)
        if not decision.allowed:
            return ToolOutcome(name=name, status="rejected",
                               reason=decision.reason, args_summary=args_sum,
                               origin="permission_gate")
        try:
            resolved, resolved_extra = self._resolve_arg_paths(args)
        except permissions.PathRejected as exc:
            return ToolOutcome(name=name, status="rejected", reason=str(exc),
                               args_summary=args_sum, origin="permission_gate",
                               audit={"permission": permissions.VERDICT_DENY,
                                      "approval_source": "deny_rule"})
        ledger = self._ctx.operation_ledger if spec.effect != "read" else None
        from core.harness.operations import args_hash
        digest = args_hash(name, args, [resolved, *resolved_extra.values()])
        if ledger and operation_id:
            replay = ledger.replay(operation_id, digest, request_id)
            if replay is not None:
                return replay
        async def operation(control):
            if ledger:
                ledger.bind(control, spec, digest)
            control.checkpoint("prepare")
            if spec.plan is None:
                return await self._execute_read(spec, args, resolved, control.async_cancel,
                                                started, args_sum, ask_emitter,
                                                session_id=session_id, request_id=request_id,
                                                control=control)
            return await self._execute_planned(spec, args, resolved, resolved_extra,
                                              control.async_cancel, started, args_sum, ask_emitter,
                                              session_id=session_id, request_id=request_id,
                                              control=control)
        outcome = await self._ctx.operations.run(
            spec, [resolved, *resolved_extra.values()], operation, cancel_event,
            self._ctx.timeout_seconds, self._ctx.registry, request_id, operation_id=operation_id)
        if organization:
            attach_receipt(outcome, resolved, resolved_extra.get("dest"))
        return outcome

    # ---- 只读工具路径（R20 整机只读）+ E 项 .env 读分流（R25 补充⑫） -----------------

    async def _execute_read(self, spec: ToolSpec, args: dict, resolved: Path,
                            cancel_event: asyncio.Event, started: int, args_sum: str,
                            ask_emitter, *, session_id: str = "",
                            request_id: str = "", control=None) -> ToolOutcome:
        if not permissions.is_env_path(resolved):
            return await self._run_with_race(spec, args, resolved, cancel_event,
                                             started, args_sum, control=control)
        # .env（含 *.env / *.env.*）：读已放开（对 R20 的显式修订），写仍硬拒；
        # 项目内读 ALLOW，项目外读 ASK；每次读取落 credential_read 审计且预览脱敏。
        audit_fields: dict = {"credential_read": True, "target": str(resolved),
                              "permission": permissions.VERDICT_ALLOW}
        if permissions.location_of(resolved, self._ctx.project_root) == permissions.OUT_ROOT:
            gate = self._ctx.ask_gate
            if gate is None:
                return ToolOutcome(name=spec.name, status="rejected",
                                   reason="需要确认但询问通道未装配——按拒绝处理（禁静默放行）",
                                   args_summary=args_sum, origin="ask_gate",
                                   audit={"permission": permissions.VERDICT_DENY,
                                          "approval_source": "ask_unavailable",
                                          "credential_read": True, "target": str(resolved)})
            question = AskQuestion(
                tool=spec.name, action_category="read", location_category="out_root",
                lines=[f"⚠ 需要确认：读取项目外配置文件 {resolved}"
                       "（可能含密钥等敏感值；仅限用户明确要求时读取）",
                       "  1 允许   2 拒绝   3 本会话内“这类动作”都允许"])
            if ask_emitter is not None:
                await ask_emitter({"tool": spec.name, "lines": question.lines,
                                   "action_category": question.action_category,
                                   "location_category": question.location_category,
                                   "source": "model"})
            answer = await _ask_user(gate, question, session_id=session_id,
                                     request_id=request_id,
                                     project_root=self._ctx.project_root,
                                     cancel_event=cancel_event)
            if not answer.approved:
                return ToolOutcome(
                    name=spec.name, status="rejected",
                    reason=(f"需要确认的读取未获允许：{gate.deny_label(answer.approval_source)}"
                            "（项目外凭据类文件读取须确认）"),
                    args_summary=args_sum, origin="ask_gate",
                    audit={"permission": permissions.VERDICT_DENY,
                           "approval_source": answer.approval_source,
                           "credential_read": True, "target": str(resolved)})
            audit_fields["approval_source"] = answer.approval_source
        else:
            audit_fields["approval_source"] = "allow_rule"
        outcome = await self._run_with_race(spec, args, resolved, cancel_event,
                                            started, args_sum, audit_fields=audit_fields, control=control)
        outcome.redact_result_preview = True
        return outcome

    # ---- 路径解析 ------------------------------------------------------------------

    def _resolve_arg_paths(self, args: dict) -> tuple[Path, dict[str, Path]]:
        raw = args.get("path")
        base = self._ctx.cwd or self._ctx.project_root
        resolved = (permissions.resolve_read_path(base, str(raw))
                    if raw is not None else base)
        extra: dict[str, Path] = {}
        for key in _EXTRA_PATH_KEYS:
            if args.get(key) is not None:
                extra[key] = permissions.resolve_read_path(base,
                                                           str(args[key]))
        return resolved, extra

    def _skip_filter(self, approved_path=None):
        """发现工具沿用读取权限：根内 .env 可见；根外须显式读取并经过 ASK。"""
        def _skip(p: Path) -> bool:
            if ".git" in p.parts or ".venv" in p.parts or p.name in (".git", ".venv"):
                return True
            if (p != approved_path and permissions.is_env_path(p) and
                    permissions.location_of(p, self._ctx.project_root) == permissions.OUT_ROOT):
                return True
            return permissions.check_credential_path(p) is not None
        return _skip

    # ---- 写操作组：判定 → 备份 → 询问 → 执行 ---------------------------------------

    async def _execute_planned(self, *args, **kwargs) -> ToolOutcome:
        from core.tools.planned_execution import execute_planned
        return await execute_planned(self, *args, **kwargs)

    # ---- 三路竞速执行（取消 / 超时 / 完成） ------------------------------------------

    async def _run_with_race(self, spec, args, resolved, cancel_event, started, args_sum,
                             *, resolved_extra=None, audit_fields=None, control=None):
        # 竞速与活跃引用由外层 OperationRunner 统一管理。
        return await run_sync(spec, args, resolved, self._ctx.project_root,
                              self._skip_filter(resolved), args_sum, resolved_extra,
                              audit_fields, control, data_dir=self._ctx.data_dir,
                              artifacts=self._ctx.artifacts)


async def _ask_user(gate: AskGate | None, question: AskQuestion, *,
                    session_id: str, request_id: str, project_root,
                    cancel_event: asyncio.Event) -> AskAnswer:
    """B08 询问交互（模块级守 V6）：构造 AuthContext → gate.ask（含取消竞速）。

    授权键与并发绑定由 gate 统一持有；scope 三值判定仍在 permissions/执行层先行。
    """
    if gate is None:
        return AskAnswer(False, "ask_unavailable")
    auth = build_auth(question, session_id=session_id, request_id=request_id,
                      project_scope=str(project_root))
    ask_task = asyncio.create_task(gate.ask(auth, question))
    cancel_task = asyncio.create_task(cancel_event.wait())
    done, _ = await asyncio.wait({ask_task, cancel_task},
                                 return_when=asyncio.FIRST_COMPLETED)
    if not cancel_task.done():
        cancel_task.cancel()
    if ask_task.done():
        return ask_task.result()
    ask_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await ask_task
    return AskAnswer(False, "user_cancel")


def _denied_outcome(name: str, reason: str, source: str, args_sum: str,
                    plan, started: int, *, origin: str = "permission_gate") -> ToolOutcome:
    return ToolOutcome(name=name, status="rejected", reason=reason,
                       duration_ms=(time.monotonic_ns() - started) // 1_000_000,
                       args_summary=args_sum, origin=origin,
                       audit={"action": plan.action, "permission": permissions.VERDICT_DENY,
                              "approval_source": source, "count": plan.target_count,
                              "overwrite": plan.overwrite})


def _build_question(name: str, plan, decision, resolved: Path,
                    backup_path: Path | None) -> AskQuestion:
    label = {"create": "新建", "edit": "覆盖写入" if plan.overwrite else "编辑",
             "move": "移动并覆盖目标" if plan.overwrite else "移动",
             "delete": "删除"}.get(plan.action, plan.action)
    note = "已自动备份" if backup_path is not None else (
        "可逆删除" if plan.reversible_delete else "审计照常")
    lines = [f"⚠ 需要确认：{label} {resolved}（{plan.detail}，{note}）",
             "  1 允许   2 拒绝   3 本会话内“这类动作”都允许"]
    return AskQuestion(tool=name, action_category=decision.action_category,
                        location_category=decision.location_category, lines=lines)
