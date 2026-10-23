"""Vastar runtime component."""

from __future__ import annotations

import asyncio
import itertools
import uuid
from collections import deque
from dataclasses import dataclass, field

from agents.definition import AgentDefinition
from core.events import EventFactory, EventSequencer, EventType, Producer
from core.llm.base import ProviderSpec
from core.llm.retry import RetryPolicy
from core.model_switch import ModelSwitchMixin
from core.prompt import PromptBundle
from core.runtime import ReActRuntime, RunRequest, RunResult
from core.session_compact import CompactSessionMixin
from core.session_lifecycle import RunLifecycleMixin
from core.session_resume import SessionResumeMixin
from core.skills.session_hook import SkillSessionMixin
from memory.store import Store
from memory.writer import Origin, StoreWriter


@dataclass
class PendingInput:
    text: str
    message_id: str
    origin: str
    submit_seq: int
    interrupt: bool = False
    retry_of_run_id: str | None = None
    run_id: str | None = None
    explicit_skill: str | None = None   # 提交时绑定，不能让此前排队输入消费后来的安排
    continuation_of: str | None = None
                                         # run_id；此时 text 为空、不落新用户消息）
    sequencer: EventSequencer = field(default_factory=EventSequencer)   # B03.01：一个 Run
                                         # 一个分配器——提交侧（input.accepted）与派发侧
                                         #（runtime 事件）共用，event_id 不撞号


@dataclass
class InputReceipt:
    input_id: str
    submit_seq: int
    persisted: bool
    state: str                 # accepted（即将执行）/ queued（排队中）
    queue_position: int = 0
    note: str = ""


class SessionCoordinator(SessionResumeMixin, RunLifecycleMixin, ModelSwitchMixin, CompactSessionMixin,
                         SkillSessionMixin):   # B11：/skill 显式通道（外挂 Mixin，D10）
    """会话协调器：一个会话最多一个活动 Run；事件单入口按序处理。"""

    def __init__(
        self,
        *,
        store: Store,
        writer: StoreWriter,
        renderer,
        runtime: ReActRuntime,
        agent_def: AgentDefinition,
        primary: ProviderSpec,
        fallback: ProviderSpec | None,
        retry_policy: RetryPolicy,
        prompt_bundle: PromptBundle,
        token_cfg: dict,
        context_cfg: dict,
        config_id: str,
        origin: str = Origin.USER,
        session_id: str | None = None,
        model_catalog: list[tuple[str, str]] | None = None,
        spec_factory=None,
        skills=None,
    ) -> None:
        self._store = store
        self._writer = writer
        self._renderer = renderer
        self._runtime = runtime
        self._agent_def = agent_def
        self._primary = primary
        self._fallback = fallback
        self._retry_policy = retry_policy
        self._prompt_bundle = prompt_bundle
        self._token_cfg = token_cfg
        self._context_cfg = context_cfg
        self._config_id = config_id
        self._origin = origin
        self._model_catalog = list(model_catalog or [])   # [(provider, model)] 全部可选项
        self._spec_factory = spec_factory                 # (provider, model) -> ProviderSpec
        self._skills = skills                             # B11：SkillsContext（None = 技能关闭）

        self.session_id = session_id or f"sess-{uuid.uuid4().hex}"
        self.mode = agent_def.session_mode
        self.paused = False
        self.late_events_dropped = 0
        self.last_result: RunResult | None = None

        self._pending: deque[PendingInput] = deque()
        self._seq = itertools.count(1)
        self._active: RunRequest | None = None
        self._wake = asyncio.Event()
        self._stopped = False
        self._pause_after_interrupt = False
        self._cancel_pending = False
        self._last_user_input: str | None = None

    # ---- 生命周期 ---------------------------------------------------------------

    def ensure_session(self) -> None:
        self._store.create_session(
            self.session_id, mode=self.mode,
            config_version=self._config_id, agent_version=self._agent_def.version,
        )
        from core.harness.durable_session import restore_pending
        restore_pending(self)
        from core.memory_session import freeze_memory
        freeze_memory(self)

    async def run_forever(self) -> None:
        """派发循环：取队首 → 冻结历史 → 执行 Run → 终态处理。"""
        self.ensure_session()
        try:
            while not self._stopped:
                if self._active is not None or not self._pending or self._dispatch_blocked():
                    self._wake.clear()
                    await self._wake.wait()
                    continue
                item = self._pending.popleft()
                await self._dispatch(item)
        finally:
            if await self._writer.close() != "confirmed":
                self._renderer.render_error(self._writer.storage_error())

    def _dispatch_blocked(self) -> bool:
        if not self._pending:
            return True
        if not self.paused:
            return False
        # 暂停时仅放行 interrupt 输入（取消收尾后自动执行的那条）
        return not self._pending[0].interrupt

    def stop(self) -> None:
        self._stopped = True
        if self._active is not None:
            self._active.cancel_event.set()   # 收尾连带取消活动 Run（P0-9.3）
        self._wake.set()

    def _persist_user_message(self, text: str, origin: str) -> tuple[str, int]:
        """Vastar runtime component."""
        submit_seq = next(self._seq)
        message_id = f"m-{uuid.uuid4().hex}"
        self._store.append_message(
            message_id, self.session_id, role="user", content=text,
            origin=origin, submit_seq=submit_seq,
        )
        return message_id, submit_seq

    # ---- 输入 -------------------------------------------------------------------

    async def submit(self, text: str, *, origin: str | None = None,
                     interrupt: bool | None = None) -> InputReceipt:
        """接受输入：先持久化后确认；活动 Run 时按模式排队或打断。"""
        origin = origin or self._origin
        message_id, submit_seq = self._persist_user_message(text, origin)
        busy = self._active is not None
        do_interrupt = bool(interrupt) if interrupt is not None else (
            busy and self.mode == "interrupt")
        run_id = f"r-{uuid.uuid4().hex}"  # 提交时分配（全局唯一）；input.accepted 随提交发出
        item = PendingInput(text=text, message_id=message_id, origin=origin,
                            submit_seq=submit_seq, interrupt=do_interrupt,
                            run_id=run_id, explicit_skill=self._consume_armed_skill())
        from core.harness.durable_session import persist_pending
        persist_pending(self, item)
        if do_interrupt:
            if not busy and self._pending:
                self._pause_after_interrupt = True
            self._pending.appendleft(item)
            if busy:
                self.request_cancel()
            receipt = InputReceipt(input_id=message_id, submit_seq=submit_seq,
                                   persisted=True, state="accepted",
                                   note="interrupt：已优先接受本输入" + ("，正在取消当前回答" if busy else ""))
        else:
            self._pending.append(item)
            position = len(self._pending) if busy else 1
            receipt = InputReceipt(
                input_id=message_id, submit_seq=submit_seq, persisted=True,
                state="queued" if busy else "accepted",
                queue_position=position if busy else 0,
                note="已排队（当前回答完成后按序执行）" if busy else "",
            )
        self._last_user_input = text
        if self.paused and self._active is None:
            # 取消后的暂停只拦"自动续跑"；用户显式提交新输入 = 明确恢复，按序派发
            self.paused = False
        # input.accepted 在提交时刻发出（不是派发时刻）——排队反馈不能等到前一条答完
        factory = EventFactory(self.session_id, run_id, 0, origin, sequencer=item.sequencer)
        await self._emit_checked(factory.build(EventType.INPUT_ACCEPTED, {
            "input_id": message_id, "submit_seq": submit_seq,
            "mode": "interrupt" if do_interrupt else "queue",
        }, producer=Producer.COORDINATOR))
        self._wake.set()
        return receipt

    def request_cancel(self) -> bool:
        """请求取消当前 Run（用户 Ctrl+C / interrupt）。

        覆盖两种状态：派发中/生成中 → 取消活动 Run；已提交但**尚未派发** →
        置 _cancel_pending，派发时预设 cancel_event，走正规 CANCELLED 收尾
        （消除"提交后立即 Ctrl+C"的派发竞态）。
        """
        if self._active is not None:
            self._active.cancel_event.set()
            return True
        if self._pending:
            self._cancel_pending = True
            self._wake.set()
            return True
        return False

    def has_active_or_pending(self) -> bool:
        """Ctrl+C 语境判定用：有活动 Run 或待派发输入都算"生成中"。"""
        return self._active is not None or bool(self._pending)

    def is_busy(self) -> bool:
        return self._active is not None

    def has_draft_queue(self) -> int:
        return len(self._pending)

    # ---- 派发与事件 ----------------------------------------------------------------

    async def new_session(self) -> str:
        self.disarm_skill()   # 显式安排属于当前会话，不得跨 /new 泄漏
        self.session_id = f"sess-{uuid.uuid4().hex}"
        self._pending.clear()
        self.paused = False
        self._runtime.rotate_ask_session(self.session_id)   # B08：旧会话临时授权就此失效
        self.ensure_session()
        return self.session_id

    def set_mode(self, mode: str) -> None:
        if mode not in ("queue", "interrupt"):
            raise ValueError(f"未知模式：{mode}")
        self.mode = mode
        self._store.set_session_mode(self.session_id, mode)

    def last_user_input(self) -> str | None:
        return self._last_user_input

    def primary_label(self) -> str:
        return f"{self._primary.name}/{self._primary.model}"

    def retry_last(self) -> None:
        """重问上一条输入：创建带 retry_of 的新 Run（旧半成品保留，不覆盖）。"""
        text = self._last_user_input
        if not text:
            return
        message_id, submit_seq = self._persist_user_message(text, self._origin)
        item = PendingInput(text=text, message_id=message_id, origin=self._origin,
                            submit_seq=submit_seq, retry_of_run_id=self.last_result.run_id
                            if self.last_result else None,
                            run_id=f"r-{uuid.uuid4().hex}")
        from core.harness.durable_session import persist_pending
        persist_pending(self, item)
        self._pending.append(item)
        self._wake.set()

    def storage_health(self) -> str:
        return "ok" if self._writer.last_error is None else "degraded"

    def storage_health_detail(self) -> str:
        """Vastar runtime component."""
        status = self._writer.storage_status()
        detail = (f"{status['health']}；pending_count={status['pending_count']}；"
                  f"durability={status['durability']}（未落盘 {status['pending_count']} 条事件）")
        if self._writer.last_error is not None:
            detail += (f"；上次写库失败：{self._writer.last_error}；"
                       "下一次成功写入后自动恢复；详情见 logs/vastar.log")
        return detail
