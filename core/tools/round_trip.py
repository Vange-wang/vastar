"""Vastar runtime component."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from functools import partial

from core.errors import ErrorCategory, ErrorCode, VastarError
from core.events import Channel, EventType
from core.harness.preflight import context_decision  # noqa: F401
from core.history_capture import execute_captured
from core.llm.base import FrameKind, LLMRequest, ProviderSpec
from core.run_state import AttemptOutcome, SharedState
from core.skills.tool_spec import USE_SKILL_TOOL
from core.tools.audit import args_summary
from core.tools.executor import ToolExecutor
from core.tools.final_answer import final_messages, record_read_round, tool_message
from core.tools.registry import ToolsContext
from core.tools.result_delivery import dispatch_result


def _skill_event_type(action: str) -> str:
    """B11：激活记录动作 → 事件类型（rejected 单列；loaded/replaced 都属加载留痕）。"""
    return EventType.SKILL_REJECTED if action == "rejected" else EventType.SKILL_LOADED


def _call_signature(name: str, args: dict) -> str:
    """调用签名（R22）：(工具名, 归一化参数)。

    归一化：参数键序无关（sort_keys）、path 类值按平台大小写规则归一
    （os.path.normcase）、空白容忍（json.loads 语义，R19 注意点①）。
    """
    normalized = {k: (os.path.normcase(os.path.normpath(v)) if isinstance(v, str) and k in {"path", "dest"} else v)
                  for k, v in args.items()}
    return f"{name}|{json.dumps(normalized, sort_keys=True, ensure_ascii=False)}"


_BUDGET_ADVICE = ("可操作建议：① /compact 压缩历史后重试；② 改用范围读取"
                  "（read_file 的 start_line/max_lines）减小工具结果；"
                  "③ 调大 config 的 max_input_tokens 预算后重试。")


async def _gate_request(s: SharedState, spec: ProviderSpec, llm_request: LLMRequest,
                        messages: list[dict], tools, *, attempt_id: str,
                        attempt_index: int, label: str, action_text: str) -> bool:
    """B06：请求发出**之前**的预算闸门（复用 core/token.py check_gate——不新造实现）。

    返回 False = 预算内可发出；True = 超限已显式拦截（tool.rejected 事件 + warnings
    已发；调用方不得计数、不得发出该请求——V2 禁静默，禁"先发请求后补计量"）。
    计量口径 = messages（含工具结果原文）＋ tool schema ＋ system ＋ 输出预留
    （BudgetProfile.input_budget 内含）＋ 保证余量（safety_margin_ratio，与首请求同源）。
    """
    margin = float(s.request.token_cfg.get("safety_margin_ratio", 0.1))
    decision = context_decision(s, spec, replace(llm_request, messages=messages, tools=tools))
    if decision.allowed:
        return False
    detail = (f"{label}请求未发出（INPUT_BUDGET_EXCEEDED）：估算 "
              f"{decision.estimated_tokens} × 余量 {1 + margin:.2f} = "
              f"{decision.gate_tokens} > 预算 {decision.budget}；{action_text}"
              f"{_BUDGET_ADVICE}")
    await s.emit(s.factory.build(
        EventType.TOOL_REJECTED,
        {"tool": "<next_round>", "reason": "input_budget_exceeded",
         "code": ErrorCode.INPUT_BUDGET_EXCEEDED, "detail": detail, "source": "gate"},
        channel=Channel.STATUS, attempt_id=attempt_id, request_epoch=attempt_index))
    s.warnings.append(detail)
    return True


async def _consume_segment(consume, s, spec, request, attempt_index, attempt_id, outcome):
    """成功或断流都合并已经呈现的帧；异常仍交运行层按可见内容裁决。"""
    segment = AttemptOutcome()
    buffered_before = len(s.pending_terminal_text)
    try:
        await consume(s, spec, request, attempt_index, attempt_id, segment)
    finally:
        buffered = s.pending_terminal_text[buffered_before:]
        if buffered:
            segment.frames.append({'kind': FrameKind.CONTENT, 'text': buffered, 'source_chunk_index': 0,
                                   'terminal_buffered': True})
        outcome.frames.extend(segment.frames)
        outcome.latest_frames = segment.frames
        outcome.has_content = outcome.has_content or segment.has_content or bool(s.pending_terminal_text.strip())
        outcome.has_reasoning = outcome.has_reasoning or segment.has_reasoning
        outcome.finish_reason = segment.finish_reason
        outcome.tool_calls = segment.tool_calls


class ToolRoundTripper:
    """回合内一次工具往返的执行体（依赖执行层闸门，不经本类旁路）。

    race_consume 由 AttemptRunner 注入（StreamConsumer.race_consume，B07 交付项 0 拆出后
    来源随迁）：第二跳与首跳共用同一"消费 vs 取消"竞速语义。
    """

    def __init__(self, context: ToolsContext, race_consume) -> None:
        self._tools = context
        self._executor = ToolExecutor(context)
        self._race_consume = race_consume

    async def run(self, s: SharedState, spec: ProviderSpec, llm_request: LLMRequest,
                  attempt_id: str, attempt_index: int,
                  outcome: AttemptOutcome) -> AttemptOutcome:
        """Vastar runtime component."""
        tools = self._tools
        if s.tool_rounds == 0:
            await self._emit_tool(s, "tool.policy", {"limit_mode": "unlimited" if tools.max_tool_rounds is None else "finite",
                "configured_max_rounds": tools.max_tool_rounds, "config_id": s.request.config_id},
                attempt_id, attempt_index)
        messages = list(llm_request.messages)
        while outcome.tool_calls is not None:
            if tools.max_tool_rounds is not None and s.tool_rounds >= tools.max_tool_rounds:
                await self._emit_tool(s, EventType.TOOL_REJECTED, {
                    "tool": "<next_round>", "reason": "round_limit",
                    "detail": (f"已达工具调用上限（{tools.max_tool_rounds}），"
                               "本轮不再调用工具，将以已有信息作答"),
                    "source": "model",
                }, attempt_id, attempt_index)
                return await self._force_final_answer(
                    s, spec, llm_request, attempt_id, attempt_index, outcome,
                    messages=messages)
            s.tool_rounds += 1

            calls = await self._trim_parallel(s, list(outcome.tool_calls),
                                              attempt_id, attempt_index)

            messages.append(tool_message(outcome, calls))
            batch_messages, stalled = await execute_captured(
                self, s, calls, attempt_id, attempt_index)
            messages.extend(batch_messages)
            s.terminal_tool_receipts.extend(m.get('content', '') for m in batch_messages if m.get('role') == 'tool')
            record_read_round(s, tools, calls)
            # B07：整组完成后才写入共享状态（半片不入）——供续写从当前对话构建请求
            s.committed_messages = list(messages)
            if self._waiting(s):
                outcome.finish_reason, outcome.tool_calls = "needs_input", None
                return outcome
            if stalled or self._completed(s):
                # B20：四项事实确认无进展或任务已验收后，剥离工具作答。
                return await self._force_final_answer(
                    s, spec, llm_request, attempt_id, attempt_index, outcome,
                    messages=messages)


            # 续问：同一 attempt_id，工具事件与 phase 供审计区分（Spec §10.3）
            follow_up = LLMRequest(provider=spec.name, model=spec.model,
                                   messages=messages, params=dict(spec.params),
                                   timeouts=spec.timeouts, tools=llm_request.tools,
                                   meta={**llm_request.meta, "phase": "final_answer"})
            from core.tools.rolling_context import roll_tool_history
            messages, archive_ref = roll_tool_history(s, spec, follow_up, tools)
            if archive_ref:
                follow_up = replace(follow_up, messages=messages)
                s.committed_messages = list(messages)
                await self._emit_tool(s, EventType.CONTEXT_COMPACTED, {
                    'kind': 'tool_archive', 'artifact_ref': archive_ref,
                    'detail': '旧工具结果已完整归档，可分页回读；继续执行。'},
                    attempt_id, attempt_index)
            follow_up.meta["_budget_policy"] = partial(
                _gate_request, s, spec, follow_up, messages, follow_up.tools,
                attempt_id=attempt_id, attempt_index=attempt_index, label="工具续问",
                action_text="已改以已有信息作答（剥离工具清单重试终答，消息原文完整保留——无静默截断）。")
            try:
                await _consume_segment(self._race_consume, s, spec, follow_up,
                                       attempt_index, attempt_id, outcome)
            except VastarError as err:
                if err.code != ErrorCode.INPUT_BUDGET_EXCEEDED:
                    raise
                return await self._force_final_answer(
                    s, spec, llm_request, attempt_id, attempt_index, outcome, messages=messages)
        return outcome

    async def _trim_parallel(self, s: SharedState, calls: list,
                             attempt_id: str, attempt_index: int) -> list:
        """超出单响应并行上限的调用显式拒绝（TOOL_REJECTED 留痕），返回截断后清单。

        （V6：从 run() 拆出——B07 写入 committed_messages 后 run() 超 80 行函数红线。）
        """
        if self._tools.max_calls_per_turn is None:
            return calls
        for extra in calls[self._tools.max_calls_per_turn:]:
            name = str((extra.get("function") or {}).get("name", "?"))
            await self._emit_tool(s, EventType.TOOL_REJECTED, {
                "tool": name, "reason": "parallel_limit",
                "detail": f"超出单响应并行上限 {self._tools.max_calls_per_turn}",
                "source": "model",
            }, attempt_id, attempt_index)
        return calls[:self._tools.max_calls_per_turn]

    async def _force_final_answer(self, s: SharedState, spec: ProviderSpec,
                                  llm_request: LLMRequest, attempt_id: str,
                                  attempt_index: int, outcome: AttemptOutcome,
                                  *, messages: list[dict]) -> AttemptOutcome:
        """R21 降级作答：剥离 tools 强制以已有信息作答（同 attempt；不新增 attempt 记录）。

        B06：降级请求同样先计量后发出；仍超限 → 不发出、返回可解释的部分结果
        （已产出内容保留 + 拦截说明入 warnings），**不再递归降级**。
        """
        messages = final_messages(messages)
        degraded = LLMRequest(provider=spec.name, model=spec.model, messages=messages,
                              params=dict(spec.params), timeouts=spec.timeouts,
                              tools=None,
                              meta={**llm_request.meta, "phase": "degraded_answer"})
        degraded.meta["_budget_policy"] = partial(
            _gate_request, s, spec, degraded, messages, None,
            attempt_id=attempt_id, attempt_index=attempt_index,
            label="强制终答（剥离工具清单后仍超限）",
            action_text="返回已产出内容（部分结果），不再递归降级。")
        try:
            await _consume_segment(self._race_consume, s, spec, degraded,
                                   attempt_index, attempt_id, outcome)
        except VastarError as err:
            if err.code == ErrorCode.UNPARSED_TOOL_CALL:
                err.details['phase'] = 'forced_final'
            if err.code != ErrorCode.INPUT_BUDGET_EXCEEDED:
                raise
            outcome.finish_reason, outcome.tool_calls = "input_budget_exceeded", None
            return outcome
        if outcome.tool_calls:
            # 无工具请求仍返回 tool_calls = 协议异常（禁静默；此时已无工具可回填）
            raise VastarError(code=ErrorCode.TOOL_REJECTED, category=ErrorCategory.PROTOCOL,
                              message="降级作答请求（无工具）仍返回 tool_calls——协议异常",
                              retryable=False)
        if not any(f['kind'] == 'content' and f.get('text', '').strip()
                   for f in outcome.latest_frames or []):
            raise VastarError(code=ErrorCode.NO_FINAL_CONTENT, category=ErrorCategory.PROTOCOL,
                              message='终答未产生正文；保留已有部分，不把查证前言当完成。',
                              retryable=False)
        return outcome

    async def _execute_tool_calls(self, s: SharedState, calls: list,
                                  attempt_id: str, attempt_index: int, *, messages=None
                                  ) -> tuple[list[dict], bool]:
        """执行后判定四项事实，停止时保持整组 tool 消息完整。"""
        messages = [] if messages is None else messages
        for call_index, tc in enumerate(calls):
            function = tc.get("function") or {}
            name = str(function.get("name", ""))
            raw_args = function.get("arguments") or "{}"
            try:
                args = json.loads(raw_args)          # 参数 JSON 空白容忍（R19 注意点①）
                if not isinstance(args, dict):
                    raise ValueError("参数必须是 JSON 对象")
            except ValueError as exc:
                await self._emit_tool(s, EventType.TOOL_REJECTED, {
                    "tool": name, "reason": "arguments_parse_failed",
                    "detail": str(exc), "source": "model",
                }, attempt_id, attempt_index)
                messages.append({"role": "tool", "tool_call_id": str(tc.get("id") or ""),
                                 "content": f"[参数解析失败] {exc}"})
                continue
            sig = _call_signature(name, args)
            hist = s.tool_history.get(sig) or {"requests": 0}
            await self._emit_tool(s, EventType.TOOL_REQUESTED, {
                "tool": name, "args_summary": args_summary(args), "source": "model",
            }, attempt_id, attempt_index)
            result = await self._executor.execute(
                name, args, cancel_event=s.request.cancel_event,
                ask_emitter=self._ask_emitter(s, attempt_id, attempt_index),
                session_id=s.request.session_id,
                request_id=s.request.run_id,
                operation_id=f"op-{s.request.run_id}-{tc.get('id')}")
            hist = {"requests": hist["requests"] + 1}
            
            content = await self._dispatch_result(
                s, name, result, attempt_id, attempt_index)
            if name == USE_SKILL_TOOL and self._tools.skills is not None:
                # B11：模型通道触发留痕（D8；与工具结果同轮落闸，run.ended 之前）
                for rec in self._tools.skills.drain_records():
                    s.result.skill_loads.append(rec.to_payload())
                    if rec.action != "already_active":
                        await self._emit_tool(s, _skill_event_type(rec.action),
                                              rec.to_payload(), attempt_id, attempt_index)
            if content is None or s.request.cancel_event.is_set():
                raise VastarError(code=ErrorCode.CANCELLED,
                                  category=ErrorCategory.CANCELLED,
                                  message="用户取消（工具执行中）")
            s.tool_history[sig] = hist
            messages.append({"role": "tool", "tool_call_id": str(tc.get("id") or ""),
                             "content": content})
            if await self._progress_stop(s, sig, name, result, attempt_id, attempt_index) or self._waiting(s):
                messages.extend({"role": "tool", "tool_call_id": str(rest.get("id") or ""),
                                 "content": "[工具未执行] 进展守卫或等待用户；来源：task_harness"}
                                for rest in calls[call_index + 1:])
                return messages, True
        return messages, False

    async def _dispatch_result(self, s, name, result, attempt_id, attempt_index):
        return await dispatch_result(self._emit_tool, s, name, result, attempt_id, attempt_index)

    def _ask_emitter(self, s: SharedState, attempt_id: str, attempt_index: int):
        """Vastar runtime component."""

        async def emit(payload: dict) -> None:
            await self._emit_tool(s, EventType.TOOL_ASK, payload, attempt_id, attempt_index)

        return emit

    async def _emit_tool(self, s: SharedState, event_type: str, payload: dict,
                         attempt_id: str, attempt_index: int) -> None:
        await s.emit(s.factory.build(event_type, payload, channel=Channel.STATUS,
                                     attempt_id=attempt_id, request_epoch=attempt_index))

    def _waiting(self, s):
        service = getattr(self._tools, "task_service", None)
        return bool(service and s.request.task_id and service.current(s.request.session_id)["status"] in {"WAITING_INPUT", "BLOCKED", "FAILED", "CANCELLED"})

    async def _progress_stop(self, s, sig, name, result, attempt_id, attempt_index):
        from core.harness.progress import assess_progress
        record = await assess_progress(s, self._tools, sig, name, result)
        if record["stalled"]:
            await self._emit_tool(s, EventType.TOOL_REJECTED, {
                "tool": name, "reason": "loop_no_progress(stalled)",
                "detail": "四项事实连续未变化，已停止继续调用工具", "progress": record,
                "source": "progress_guard"}, attempt_id, attempt_index)
        return record["stalled"]

    def _completed(self, s):
        service = getattr(self._tools, "task_service", None)
        return bool(service and s.request.task_id and service.current(s.request.session_id)["status"] == "COMPLETED")
