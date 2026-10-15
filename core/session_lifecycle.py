"""Vastar runtime component."""

from __future__ import annotations

import uuid

from core.events import EventType, RuntimeEvent
from core.run_state import RunRequest, RunResult, RunState


class RunLifecycleMixin:
    """Run 派发与收尾；宿主为 SessionCoordinator。"""

    async def _dispatch(self, item) -> None:
        # item: PendingInput（core.session 定义；此处鸭子类型传递以避免循环导入）
        run_id = item.run_id  # 提交时已分配（uuid，全局唯一）
        self._store.pending_state(run_id, "running")
        # 执行时才确定所接历史；排除本条输入（其原文由 ContextAssembler 单独注入，避免重复）
        
        # partial 回答（assistant）正是续写前缀，且本条不落新用户消息。
        from core.compact_runtime import CompactBoundary
        from core.context import SourceMessage
        from core.memory_session import memory_context
        from memory.forget_runtime import generation_guard

        history: list[SourceMessage] = memory_context(self) + [
            m for m in self._store.history_messages(self.session_id)
            if item.continuation_of or m.message_id != item.message_id
        ]
        
        request = RunRequest(
            run_id=run_id, session_id=self.session_id,
            input_message_id=item.message_id, input_text=item.text,
            history=history, origin=item.origin,
            retry_of_run_id=item.retry_of_run_id, primary=self._primary,
            fallback=self._fallback, retry_policy=self._retry_policy,
            prompt_bundle=self._prompt_bundle,
            token_cfg=self._token_cfg, context_cfg=self._context_cfg,
            config_id=self._config_id,
            compact_boundary=CompactBoundary(self),
            continuation_of=item.continuation_of,
            event_sequencer=item.sequencer,   # B03.01：派发侧复用提交侧分配器
            explicit_skill=item.explicit_skill,   # B11：提交时绑定到确切输入（一次性）
            memory_guard=generation_guard(self._store, self.session_id),
        )
        if self._cancel_pending:
            # Ctrl+C 先于派发到达：预设取消事件，走正规 CANCELLED 流程（零模型调用）
            request.cancel_event.set()
            self._cancel_pending = False
        self._active = request
        self._store.create_run(run_id, self.session_id, item.message_id,
                               retry_of_run_id=item.retry_of_run_id,
                               config_snapshot={
                                   "config_id": self._config_id,
                                   "provider": self._primary.name,
                                   "model": self._primary.model,
                                   "fallback": self._fallback.name if self._fallback else None,
                                   "mode": self.mode,
                                   "retry_of_run_id": item.retry_of_run_id,
                               })
        self._writer.set_answer_message_id(run_id, f"{run_id}-assistant")
        task = self._store.task_link_run(self.session_id, run_id)
        request.task_id = task["task_id"] if task else None
        try:
            from core.harness.task_execution import run_with_task
            result = await run_with_task(self, request)
        finally:
            self._active = None
        await self._finish_run(result)

    async def _emit_checked(self, event: RuntimeEvent) -> None:
        """事件单入口：迟到数据拦截（终态后该 Run 的事件不再展示/落库）。"""
        active_id = self._active.run_id if self._active is not None else None
        if event.run_id != active_id and event.type != EventType.INPUT_ACCEPTED:
            self.late_events_dropped += 1
            return
        from core.harness.task_execution import annotate_task_outcome
        from core.run_progress import record_progress
        record_progress(self, event)
        annotate_task_outcome(self._store, event)
        self._renderer.render(event)
        if event.type == EventType.TOOL_REQUESTED:
            # 副作用执行前保存操作意图；失败则阻断本次工具调用。
            await self._writer.commit(event)
        else:
            await self._writer.append(event)

    async def _finish_run(self, result: RunResult) -> None:
        """durability 落定 → 终态/快照落库 → 暂停语义（取消不自动续跑）。"""
        durability = await self._writer.flush()
        result.durability = durability
        result.last_durable_seq = self._writer.last_durable_seq(result.run_id)
        from core.harness.durable_session import checkpoint_finished
        checkpoint_finished(self, result)
        if durability != "confirmed":
            err = self._writer.storage_error()
            self._renderer.render_error(err)
        self.last_result = result
        if result.status.lower() == RunState.CANCELLED.lower():
            # 取消不自动执行下一条排队消息；interrupt 场景例外（取消收尾后自动执行打断输入）
            if any(p.interrupt for p in self._pending):
                self._pause_after_interrupt = True
                self.paused = False
            else:
                self.paused = True
                self._renderer.render_note(
                    f"已取消；队列剩余 {len(self._pending)} 条暂停"
                    + ("（提交新输入后按序继续）" if self._pending else ""))
        elif self._pause_after_interrupt:
            self.paused = True
            self._pause_after_interrupt = False
            self._renderer.render_note(
                f"打断输入已执行；队列剩余 {len(self._pending)} 条暂停"
                + ("（提交新输入后按序继续）" if self._pending else ""))

    

    def continue_last(self) -> tuple[bool, str]:
        """以上一条不完整回答为前缀续写（新 Run；DB 追加为相邻的第二条 assistant 消息，
         append-only 不改写旧行；呈现上与前段相接 = 用户侧同一条回答）。

        仅当上一条 Run 为 incomplete 且有已输出内容（partial=True）时可用；
        自动续写预算（llm.continuation.max_count）对续写 Run 同样适用。
        """
        last = self.last_result
        if last is None or last.status != "incomplete" or not last.partial:
            return False, "没有可续写的回答（上一条已完整或不存在）"
        from core.harness.task_execution import continue_precheck  
        blocker = continue_precheck(self._store, self.session_id)
        if blocker:
            return False, blocker   # 当场说清真实原因与可用动作，不再跑出一个 needs_input 空 Run
        from core.session import PendingInput  # 局部导入：避免与 core.session 循环依赖
        item = PendingInput(
            text="", message_id=self._store.input_message_id_of(last.run_id) or "",
            origin=self._origin, submit_seq=next(self._seq),
            continuation_of=last.run_id, run_id=f"r-{uuid.uuid4().hex}")
        self._pending.append(item)
        from core.harness.durable_session import persist_pending
        persist_pending(self, item)
        if self.paused and self._active is None:
            self.paused = False   # 显式续写 = 明确恢复（与 submit 同口径）
        self._wake.set()
        return True, "已请求续写上一回答（沿用同一上下文与已输出内容为前缀）…"
