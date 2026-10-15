"""Vastar runtime component."""

from __future__ import annotations

import asyncio
import json

from core.context import ContextResult, prepare_context
from core.errors import ErrorCategory, ErrorCode, VastarError
from core.events import Channel, EventFactory, EventType
from core.llm.base import ProviderSpec
from core.llm.retry import (
    MAX_TOTAL_ATTEMPTS,
    AttemptState,
    RetryAction,
    RetryDecision,
    RetryPolicy,
)

# RunRequest/RunResult 兼作再导出：core/session.py 与 tests/fakes.py 经 core.runtime 引用
from core.run_state import (
    InternalStateError,
    RunRequest,
    RunResult,
    RunState,
    SharedState,
    StateMachine,
)
from core.runtime_attempt import AttemptRunner
from core.tools.registry import ToolsContext

DEFAULT_RUN_TOTAL_S = 900.0   # A30：Run 级总超时缺省；provider timeouts 可覆盖。


def _capability_note(enabled: list[str], permission_mode: str = "ask") -> str:
    """提示与工具清单一致性（R21/R25，V1）：按启用清单与权限模式动态注入。

    开态不得声称"没有任何工具/只读"；写操作组已开（R25）：备份/可逆删除/硬拒清单
    与询问语义如实说明——说明必须与 core/tools/permissions.py 的判定一致。
    """
    if not enabled:
        return ("你目前没有任何工具：不能读写文件、不能联网检索、不能执行代码、"
                "不能操作数据库。用户询问需要工具的任务时，明确说明当前版本不具备"
                "该能力，不要假装执行。")
    names = "、".join(enabled)
    head = (f"当前已启用工具：{names}。可读写文件与目录：覆盖/改写前自动备份、"
            "删除可逆恢复；凭据类、.git 内部、系统目录、config/logs/memory 始终拒绝。")
    if permission_mode == "auto":
        tail = ("写操作自动执行、不再逐次询问。优先直接回答用户，仅在需要文件/目录"
                "信息时调用工具。")
    else:
        tail = ("项目内常规读写直接执行；项目外、删除与批量操作会先向用户确认"
                "（用户回答 1 允许 / 2 拒绝 / 3 本会话内同类动作都允许）。"
                "优先直接回答用户，仅在需要时调用工具。")
    return head + tail


class ReActRuntime:
    """单步运行时（R18/R22）：`max_model_steps=1` 语义 = 一个**决策回合**；
    回合内工具调用由 B20 四项事实共同判断进展性（参数/观察/资源/步骤证据；
    第二次重复不拦），`tools.max_tool_rounds` 按配置设限（缺键为 10，null 不设本地轮数上限）。启用工具时一次输入的模型调用数随进展而定，不出现自主循环。"""

    def __init__(self, answer_message_id_factory, tools: ToolsContext | None = None,
                 env_facts: str = "", continuation=None, skills=None) -> None:
        self._answer_id = answer_message_id_factory  # (session_id, run_id) -> message_id
        self._tools = tools                    # None = 工具能力关闭（default-closed，行为同阶段一）
        self._env_facts = env_facts            
        self._skills = skills                  # B11：SkillsContext（None = 技能能力关闭）
        
        self._runner = AttemptRunner(self._prepare_context, tools=tools, continuation=continuation)

    async def run(self, request: RunRequest, emit) -> RunResult:
        result = RunResult(run_id=request.run_id)
        factory = EventFactory(request.session_id, request.run_id, 0, request.origin,
                               sequencer=request.event_sequencer)   # B03.01：共用提交侧分配器
        shared = SharedState(
            request=request, emit=emit, factory=factory,
            sm=StateMachine(factory, emit), result=result,
            errors=[],
        )
        try:
            total_s = DEFAULT_RUN_TOTAL_S
            if request.primary:
                from core.limits import optional_limit
                total_s = optional_limit(request.primary.timeouts.get("run_total_s", DEFAULT_RUN_TOTAL_S))
            shared.run_total_s = total_s   
                                           # run_total_timeout 正常结束 + 如实标注
            if self._skills is not None:
                # B11：每 Run 一个激活门；关键词先、显式后（显式优先级高可替换留痕）
                self._skills.begin_run(
                    request.run_id,
                    input_text="" if request.continuation_of else request.input_text,
                    explicit_skill=request.explicit_skill, task_id=request.task_id)
            async with asyncio.timeout(total_s):
                await self._execute(shared)
        except TimeoutError:
            await self._runner.on_run_total_timeout(shared)
        except InternalStateError as exc:  # 防御：状态机不一致不外泄
            shared.result.errors.append({
                "code": "INTERNAL_STATE", "category": "internal",
                "message": str(exc), "retryable": False, "details": {},
            })
            shared.result.status = shared.result.status or "failed"
        finally:
            if self._skills is not None:
                # B11：关闭本 Run 激活门；加载事实已随事件发射镜像进 result.skill_loads
                # （只冻结真正注入/投递过的记录——准备失败时不得虚报已加载）
                self._skills.end_run()
        result.model_calls = shared.model_calls   # 本轮模型调用次数（成本可观测，Spec §6）
        result.tool_rounds = shared.tool_rounds   # 工具往返轮数（R22：兜底计数按 Run 累计）
        result.tool_executions = shared.tool_exec_count   # 工具执行次数（R22 成本可见）
        return result

    # ---- 主流程 -----------------------------------------------------------------

    async def _execute(self, s: SharedState) -> None:
        request = s.request
        await s.sm.transition(RunState.PREPARING, "run_start")
        spec = request.primary
        try:
            ctx_result = self._prepare_context(request, spec)
        except VastarError as err:
            await self._runner.fail_before_request(s, err)
            return
        s.result.context_record = ctx_result.snapshot.to_record()
        s.warnings.extend(ctx_result.warnings)
        s.answer_message_id = self._answer_id(request.session_id, request.run_id)
        s.result.answer_message_id = s.answer_message_id
        await self._emit_skill_events(s)   # B11：显式/关键词通道的触发留痕（模型通道在 round_trip）
        await self._attempt_loop(s, spec, ctx_result)

    def rotate_ask_session(self, session_id: str) -> None:
        """B08：/new 生命周期点的最小接线——把新会话 id 通知询问闸门。

        切换后旧会话的挂起询问如有迟到批准，由闸门按 session_changed 显式拒绝
        （授权存储与判定都在 gate，本层不持有权限表）。
        """
        if self._tools is not None and self._tools.ask_gate is not None:
            self._tools.ask_gate.rotate_session(session_id)

    async def _emit_skill_events(self, s: SharedState) -> None:
        """B11：把激活门的增量记录落成 skill.loaded/skill.rejected 事件（D8 审计）。"""
        if self._skills is None:
            return
        for rec in self._skills.drain_records():
            s.result.skill_loads.append(rec.to_payload())   # 冻结到 Run（C.2：来源/hash/版本/触发）
            if rec.action == "already_active":
                continue   # 重复触发不重复留事件（D4③：只加载一次；记录仍在 skill_loads）
            etype = (EventType.SKILL_REJECTED if rec.action == "rejected"
                     else EventType.SKILL_LOADED)
            await s.emit(s.factory.build(etype, rec.to_payload(), channel=Channel.STATUS))

    def _prepare_context(self, request: RunRequest, spec: ProviderSpec) -> ContextResult:
        schema_text = None
        if self._tools is not None:
            # 工具清单（name/description/parameters）计入 token 预算（Spec §7）
            note = _capability_note(self._tools.registry.enabled,
                                    self._tools.permission_mode)
            if self._tools.registry.enabled:
                schema_text = json.dumps(self._tools.registry.to_openai_tools(),
                                         ensure_ascii=False)
        else:
            note = _capability_note([])
        if self._env_facts:
            
            note = f"{note}\n{self._env_facts}"
        if self._skills is not None:
            # B11：常驻清单（name+description）+ 被选中技能正文（D5/D6，全量计 token）
            note = self._skills.augment_note(note)
        kwargs = dict(
            prompt_bundle=request.prompt_bundle,
            history=request.history,
            
            current_input="" if request.continuation_of else request.input_text,
            profile=spec.profile,
            safety_margin_ratio=float(request.token_cfg.get("safety_margin_ratio", 0.1)),
            warn_threshold_ratio=float(request.context_cfg.get("warn_threshold_ratio", 0.75)),
            strict=request.strict_gate,
            approved_compact=request.approved_compact,
            tool_schema_text=schema_text,
            capability_note=note,
        )
        if request.compact_boundary is not None:
            return request.compact_boundary(request, kwargs)
        return prepare_context(**kwargs)

    def _decide(self, policy: RetryPolicy | None, spec: ProviderSpec, err: VastarError,
                provider_attempts: int, used_fallback: bool) -> RetryDecision:
        if policy is None:
            return RetryDecision(RetryAction.FAIL, reason="无重试策略")
        return policy.decide(err, AttemptState(
            provider=spec.name, attempt_index=provider_attempts,
            used_fallback=used_fallback))

    async def _refresh_retry_context(self, s, spec):
        """重试重新装配当前激活正文并过预算门，不能复用模型加载前的旧清单。"""
        try:
            ctx = self._prepare_context(s.request, spec)
        except VastarError as err:
            await self._runner.record_error(s, err, "")
            await self._runner.terminate(s, RunState.FAILED, err.code)
            return None
        s.result.context_record = ctx.snapshot.to_record()
        s.warnings.extend(ctx.warnings)
        return ctx

    async def _attempt_loop(self, s: SharedState, spec: ProviderSpec,
                            ctx_result: ContextResult) -> None:
        policy = s.request.retry_policy
        used_fallback = False
        provider_attempts = 0
        while True:
            if len(s.result.attempt_ids) >= MAX_TOTAL_ATTEMPTS:  # 防御：总尝试硬上限
                await self._runner.terminate(s, RunState.FAILED, "max_attempts_exceeded")
                return
            if s.request.cancel_event.is_set():
                # 预派发取消（Ctrl+C 竞态收口）：零 attempt、零模型调用
                await self._runner.cancel_path(s)
                return
            attempt_counter = len(s.result.attempt_ids) + 1
            attempt_id = f"{s.request.run_id}-a{attempt_counter}"
            outcome, fail_err = await self._runner.attempt_once(
                s, spec, attempt_id, attempt_counter, ctx_result)
            if fail_err is None and outcome is not None and await self._runner.classify_stream_end(
                    s, outcome, spec, ctx_result):   
                return
            if fail_err is None:
                fail_err = VastarError(
                    code=ErrorCode.PROTOCOL_PARSE_FAILED, category=ErrorCategory.PROTOCOL,
                    message="流结束但无合法结束原因（无内容）", retryable=True)
            await self._runner.record_error(s, fail_err, attempt_id)
            provider_attempts += 1                  # 当前 provider 已失败次数（含本次）
            if fail_err.category == ErrorCategory.CANCELLED or s.request.cancel_event.is_set():
                await self._runner.cancel_path(s)
                return
            if fail_err.code == ErrorCode.UNPARSED_TOOL_CALL and fail_err.details.get('phase') == 'forced_final':
                s.result.partial = bool(outcome and (outcome.has_content or outcome.has_reasoning))
                await self._runner.terminate(s, RunState.FAILED, fail_err.code)
                return
            # F07 断流分类（B05.02，行为冻结）：已产生**可见输出**（正文/思考）→ 保留并收
            # INCOMPLETE、**不重试**（重试 = 用户看见重复正文）；仅"尚无可见输出"
            # （空 / 仅工具片段——tool deltas 不落屏）且错误可重试时才进重试决策。
            if outcome is not None and (outcome.has_content or outcome.has_reasoning):
                s.result.partial = True
                await self._runner.terminate(s, RunState.INCOMPLETE, fail_err.code)
                return
            decision = self._decide(policy, spec, fail_err, provider_attempts, used_fallback)
            if used_fallback and decision.action == RetryAction.RETRY_SAME:
                decision = RetryDecision(
                    RetryAction.FAIL, reason="备用 provider 只有 1 次尝试机会（采纳件 §7）")
            if decision.action == RetryAction.RETRY_SAME:
                if await self._runner.retry_wait(s, decision.wait_s):
                    await s.sm.transition(RunState.CANCELLING, "cancel_during_retry_wait")
                    await self._runner.cancel_path(s)
                    return
                ctx_result = await self._refresh_retry_context(s, spec)
                if ctx_result is None:
                    return
                continue
            if decision.action == RetryAction.SWITCH and s.request.fallback is not None:
                switched = await self._runner.switch_provider(s, spec, fail_err)
                if switched is None:
                    await self._runner.terminate(s, RunState.FAILED, fail_err.code)
                    return
                spec, ctx_result = switched
                used_fallback, provider_attempts = True, 0
                continue
            await self._runner.terminate(s, RunState.FAILED, fail_err.code)
            return
