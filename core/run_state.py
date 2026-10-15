"""Vastar runtime component."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field

from core.context import CompactSnapshot
from core.events import EventFactory, EventSequencer, EventType
from core.llm.base import ProviderSpec
from core.llm.retry import RetryPolicy
from core.prompt import PromptBundle


class RunState:
    QUEUED = "QUEUED"
    PREPARING = "PREPARING"
    COMPACTING = "COMPACTING"
    REQUESTING = "REQUESTING"
    STREAMING = "STREAMING"
    CONTINUING = "CONTINUING"    
    RETRY_WAIT = "RETRY_WAIT"
    SWITCHING = "SWITCHING"
    CANCELLING = "CANCELLING"
    FINALIZING = "FINALIZING"
    COMPLETED = "COMPLETED"
    INCOMPLETE = "INCOMPLETE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"


TERMINAL_STATES = {
    RunState.COMPLETED, RunState.INCOMPLETE, RunState.FAILED,
    RunState.CANCELLED, RunState.INTERRUPTED,
}

_ALLOWED = {
    (RunState.QUEUED, RunState.PREPARING),
    (RunState.PREPARING, RunState.REQUESTING), (RunState.PREPARING, RunState.FINALIZING),
    (RunState.REQUESTING, RunState.STREAMING),
    (RunState.REQUESTING, RunState.RETRY_WAIT), (RunState.REQUESTING, RunState.SWITCHING),
    (RunState.REQUESTING, RunState.CANCELLING), (RunState.REQUESTING, RunState.FINALIZING),
    (RunState.STREAMING, RunState.FINALIZING), (RunState.STREAMING, RunState.CANCELLING),
    (RunState.STREAMING, RunState.CONTINUING),   
    (RunState.CONTINUING, RunState.REQUESTING),  # 续写发起新请求（同一 Run）
    (RunState.CONTINUING, RunState.FINALIZING), (RunState.CONTINUING, RunState.CANCELLING),
    (RunState.RETRY_WAIT, RunState.REQUESTING), (RunState.RETRY_WAIT, RunState.CANCELLING),
    (RunState.SWITCHING, RunState.REQUESTING), (RunState.SWITCHING, RunState.FINALIZING),
    (RunState.SWITCHING, RunState.CANCELLING),
    (RunState.CANCELLING, RunState.FINALIZING),
}
_ALLOWED |= {(RunState.FINALIZING, t) for t in TERMINAL_STATES}


class InternalStateError(Exception):
    pass


class StateMachine:
    """Run 状态机：转换合法性 + 单终态守卫。"""

    def __init__(self, factory: EventFactory, emit) -> None:
        self.state: str | None = None
        self.terminal: str | None = None
        self._factory = factory
        self._emit = emit

    async def transition(self, to: str, reason_code: str) -> None:
        frm = self.state
        if self.terminal is not None:
            raise InternalStateError(f"Run 已终态 {self.terminal}，不得再转 {to}")
        if frm is not None and (frm, to) not in _ALLOWED:
            raise InternalStateError(f"非法状态转换 {frm} → {to}")
        self.state = to
        if to in TERMINAL_STATES:
            self.terminal = to
        await self._emit(self._factory.build(
            EventType.RUN_STATE_CHANGED, {"from": frm, "to": to, "reason_code": reason_code},
        ))


@dataclass
class RunRequest:
    run_id: str
    session_id: str
    input_message_id: str
    input_text: str
    history: list  # list[SourceMessage]
    origin: str = "user"
    retry_of_run_id: str | None = None
    submitted_mono_ns: int = field(default_factory=time.monotonic_ns)
    primary: ProviderSpec | None = None
    fallback: ProviderSpec | None = None
    retry_policy: RetryPolicy | None = None
    prompt_bundle: PromptBundle | None = None
    token_cfg: dict = field(default_factory=dict)
    context_cfg: dict = field(default_factory=dict)
    config_id: str = ""
    task_id: str | None = None
    step_id: str | None = None
    strict_gate: bool = False        # True = 要求可信 token 上界（阶段一恒拒绝）
    approved_compact: CompactSnapshot | None = None  
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    event_sequencer: EventSequencer | None = None  # B03.01：一个 Run 共用的序号分配器
                                                   #（提交侧建、派发侧复用；None = 各自
                                                   # 独立计数，仅直造 factory 的场景）
    continuation_of: str | None = None  
                                        # 此时 input_text 为空、不投影 user 消息，历史末尾
                                        # 的 partial 回答即续写前缀）
    explicit_skill: str | None = None   # B11：/skill 显式通道（一次性；优先级最高，D4）
    memory_guard: object | None = None  # B23：每次模型入口前检查上下文代次。
    compact_boundary: object | None = None  # B24：安全请求边界的持久 compact 服务。


@dataclass
class RunResult:
    run_id: str
    status: str = ""
    finish_reason: str = ""
    answer_message_id: str | None = None
    partial: bool = False
    terminal_note: str = ''  # A30 unfinished-work statement, separate from reusable answer prose.
    context_outcome: str = "unchanged"
    compression_report_id: str | None = None
    degraded: bool = False
    degradation_reasons: list[str] = field(default_factory=list)
    attempt_ids: list[str] = field(default_factory=list)
    usage_totals: dict = field(default_factory=dict)
    errors: list[dict] = field(default_factory=list)
    durability: str | None = None
    last_durable_seq: int | None = None
    context_record: dict | None = None
    model_calls: int = 0               # 本轮模型调用次数（启用工具后最多 1+链深，成本可观测）
    tool_rounds: int = 0               # 工具往返轮数（R22 兜底计数，按 Run 累计不复位）
    tool_executions: int = 0           # 本轮工具执行次数（R22：/status 成本可见）
    continuations: int = 0             
                                       # llm.continuation.max_count，与 max_attempts_per_step 无关）
    usage_reports: int = 0             # B05：收到 usage 帧次数（≠ model_calls 发起数；
                                       # 0 且发起数 > 0 → 用量 unknown，禁当 0）
    skill_loads: list[dict] = field(default_factory=list)  # B11：本 Run 技能触发记录
                                       #（来源/hash/版本/触发原因，随 run.ended 冻结）
    call_records: list[dict] = field(default_factory=list)
    usage_status: str = "unknown"
    cost_summary: dict = field(default_factory=dict)
    budget_summary: dict = field(default_factory=dict)


@dataclass
class AttemptOutcome:
    has_content: bool = False
    has_reasoning: bool = False
    tool_calls: list | None = None
    finish_reason: str | None = None
    frames: list[dict] = field(default_factory=list)
    latest_frames: list[dict] | None = None  # Exact last supplier segment, not aggregate attempt.


@dataclass
class SharedState:
    """一次 Run 的共享执行上下文（收敛参数传递）。"""

    request: RunRequest
    emit: object
    factory: EventFactory
    sm: StateMachine
    result: RunResult
    latencies: dict = field(default_factory=dict)
    usage_totals: dict = field(default_factory=dict)
    errors: list[dict] = field(default_factory=list)
    frames_by_attempt: dict = field(default_factory=dict)
    answer_message_id: str | None = None
    warnings: list[str] = field(default_factory=list)
    ended_emitted: bool = False
    terminal_delivery_done: bool = False  # A30: one evidence/unfinished-work delivery.
    pending_terminal_text: str = ''  # Safe terminal body buffered before a real interruption.
    terminal_tool_receipts: list[str] = field(default_factory=list)  # This Run's committed groups only.
    degraded_to: str | None = None  
    tool_rounds: int = 0            # 工具往返轮数（R22：兜底计数，按 Run 累计不复位）
    read_only_rounds: int = 0       # TASK96: consecutive rounds without non-read tool work.
    tool_exec_count: int = 0        # 工具执行次数（R22：/status 成本可见）
    tool_history: dict = field(default_factory=dict)   # R22 死循环检测：sig → 请求数/摘要
    model_calls: int = 0            # 本轮模型调用次数（1 次产出 + ≤链深次续问）
    run_total_s: float | None = 0.0 # null = no local Run deadline; explicit quotas still apply.
                                    # 续写共享该预算不复位；0 = 未设置）
    committed_messages: list | None = None   # B07：round_trip 维护的当前对话已提交
                                    # 消息序列（成组 assistant(tool_calls)+tool replies；
                                    # 半片不入）。None = 本 attempt 未发生工具往返，
                                    # 续写请求构建回退 Run 初始快照
    call_ledger: dict = field(default_factory=dict)
    call_budget: dict | None = None  # Run 级冻结预算；切备/续写不重新初始化。
    call_admission_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _request_hash(model: str, params: dict, messages: list[dict]) -> str:
    payload = json.dumps([model, params, messages], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _latency(latencies: dict, key: str, since_ns: int) -> None:
    if key not in latencies:
        latencies[key] = time.monotonic_ns() - since_ns


def _pick(d: dict, *keys: str) -> int:
    for k in keys:
        if d.get(k) is not None:
            return int(d[k])
    return 0
