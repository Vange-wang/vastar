"""Vastar runtime component."""

from __future__ import annotations

import itertools
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime


class EventIdentityConflict(Exception):
    """同 event_id 异内容（B03.02）：事件身份冲突。

    合法重投（同 ID 同内容）走幂等跳过、不报错；同 ID 异内容必须显式失败——
    禁静默覆盖、禁静默丢弃（F01 的教训：静默丢弃让底账 emitted 9 / stored 8）。
    """


class EventSequencer:
    """一个 Run 共用的序号分配器（B03.01）。

    提交侧与派发侧的 EventFactory 共用同一实例 → seq 严格单调、event_id 不撞号；
    Run ID 的进程内高水位由锁保护，独立实例也不能重复分配；
    跨进程恢复仍由持久层读取 MAX(seq)+1 作为 start，库内冲突显式报错。
    """

    def __init__(self, start=0) -> None:
        self._counter = itertools.count(start)

    _run_high_water: dict[str, int] = {}
    _lock = threading.Lock()

    def next(self, run_id: str | None = None) -> int:
        with self._lock:
            candidate = next(self._counter)
            if run_id is None:
                return candidate
            # Retain just the high-water integer for each globally unique Run ID.
            # Factory disposal must not reset identity; recovery's start is a floor.
            seq = max(candidate, self._run_high_water.get(run_id, -1) + 1)
            self._run_high_water[run_id] = seq
            return seq


_EVENT_BUSINESS_KEYS = ("session_id", "run_id", "seq", "request_epoch", "type",
                        "channel", "producer", "origin", "payload")
# timestamp_utc / monotonic_ns 属投递时刻，不进业务内容比较（B03.02 修法纪律：
# 合法重投与首投只差投递时间，不得误判为冲突）。


def _business_value(key: str, value) -> object:
    """规范化单个业务字段：payload 两侧形态不同（待写端 dict / 读回端 JSON 串），
    统一为排序 JSON 后比较。"""
    if key == "payload":
        obj = value if isinstance(value, dict) else json.loads(value)
        return json.dumps(obj, sort_keys=True, ensure_ascii=False)
    return value


def partition_fresh_events(events: list[dict], existing: dict[str, dict]) -> list[dict]:
    """Vastar runtime component."""
    fresh = []
    seen: dict[str, dict] = {}     # 批内已收 event_id → 首条事件
    for e in events:
        old = existing.get(e["event_id"])
        if old is None:
            old = seen.get(e["event_id"])
        if old is None:
            fresh.append(e)
            seen[e["event_id"]] = e
            continue
        diff = [k for k in _EVENT_BUSINESS_KEYS
                if _business_value(k, e.get(k)) != _business_value(k, old.get(k))]
        if diff:
            raise EventIdentityConflict(
                f"事件身份冲突：event_id={e['event_id']}（run_id={e.get('run_id')}）"
                f" 同 ID 异内容，差异字段={diff}")
    return fresh


class EventType:
    INPUT_ACCEPTED = "input.accepted"
    RUN_STATE_CHANGED = "run.state_changed"
    ATTEMPT_STARTED = "attempt.started"
    REASONING_DELTA = "reasoning.delta"
    CONTENT_DELTA = "content.delta"
    CONTEXT_COMPACTED = "context.compacted"
    ERROR = "error"
    USAGE_REPORTED = "usage.reported"
    MODEL_CALL_INTENT = "model_call.intent"
    MODEL_CALL_FINISHED = "model_call.finished"
    HISTORY_TOOL_EXCHANGE = "history.tool_exchange"
    RUN_ENDED = "run.ended"
    TOOL_REQUESTED = "tool.requested"      
    TOOL_POLICY = "tool.policy"
    TOOL_EXECUTED = "tool.executed"
    TOOL_REJECTED = "tool.rejected"
    TOOL_FAILED = "tool.failed"
    TOOL_CANCELLED = "tool.cancelled"
    TOOL_ASK = "tool.ask"                  
    SKILL_LOADED = "skill.loaded"          # B11：技能加载/替换（D8 审计，含触发来源）
    SKILL_REJECTED = "skill.rejected"      # B11：同级/低优先拒绝（D4：拒绝必须留痕）


class Channel:
    REASONING = "reasoning"
    CONTENT = "content"
    STATUS = "status"
    ERROR = "error"
    METRICS = "metrics"


class Producer:
    COORDINATOR = "coordinator"
    RUNTIME = "runtime"
    STORE = "store"


_REQUIRED_FIELDS = (
    "schema_version", "event_id", "session_id", "run_id", "seq",
    "request_epoch", "type", "channel", "producer", "origin",
    "timestamp_utc", "monotonic_ns", "payload",
)
# step_index / attempt_id 按信封规范可空（int | None / str | None）

SCHEMA_VERSION = 1   


_KNOWN_TYPES = {
    EventType.INPUT_ACCEPTED, EventType.RUN_STATE_CHANGED, EventType.ATTEMPT_STARTED,
    EventType.REASONING_DELTA, EventType.CONTENT_DELTA, EventType.CONTEXT_COMPACTED,
    EventType.ERROR, EventType.USAGE_REPORTED, EventType.RUN_ENDED,
    EventType.TOOL_POLICY, EventType.TOOL_REQUESTED, EventType.TOOL_EXECUTED, EventType.TOOL_REJECTED,
    EventType.TOOL_FAILED, EventType.TOOL_CANCELLED, EventType.TOOL_ASK,
    EventType.SKILL_LOADED, EventType.SKILL_REJECTED,
    EventType.MODEL_CALL_INTENT, EventType.MODEL_CALL_FINISHED,
    EventType.HISTORY_TOOL_EXCHANGE,
}

_TYPE_CHANNEL = {
    EventType.REASONING_DELTA: Channel.REASONING,
    EventType.CONTENT_DELTA: Channel.CONTENT,
    EventType.ERROR: Channel.ERROR,
    EventType.USAGE_REPORTED: Channel.METRICS,
    EventType.MODEL_CALL_INTENT: Channel.METRICS,
    EventType.MODEL_CALL_FINISHED: Channel.METRICS,
    EventType.HISTORY_TOOL_EXCHANGE: Channel.STATUS,
    EventType.TOOL_POLICY: Channel.STATUS,
    EventType.TOOL_REQUESTED: Channel.STATUS,
    EventType.TOOL_EXECUTED: Channel.STATUS,
    EventType.TOOL_REJECTED: Channel.STATUS,
    EventType.TOOL_FAILED: Channel.STATUS,
    EventType.TOOL_CANCELLED: Channel.STATUS,
    EventType.TOOL_ASK: Channel.STATUS,
    EventType.SKILL_LOADED: Channel.STATUS,
    EventType.SKILL_REJECTED: Channel.STATUS,
}


@dataclass
class RuntimeEvent:
    schema_version: int
    event_id: str
    session_id: str
    run_id: str
    seq: int
    step_index: int | None
    attempt_id: str | None
    request_epoch: int
    type: str
    channel: str
    producer: str
    origin: str
    timestamp_utc: str
    monotonic_ns: int
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def validate_event(event: RuntimeEvent) -> None:
    """事件协议校验：字段齐全 + 通道/类型一致 + origin 合法。失败抛 ValueError。"""
    missing = [k for k in _REQUIRED_FIELDS if getattr(event, k, None) is None]
    if missing:
        raise ValueError(f"event missing fields: {missing}")
    if event.type not in _KNOWN_TYPES:
        raise ValueError(f"unknown event type: {event.type}")
    expected_channel = _TYPE_CHANNEL.get(event.type)
    if expected_channel is not None and event.channel != expected_channel:
        raise ValueError(
            f"type {event.type} requires channel {expected_channel}, got {event.channel}"
        )
    if event.origin not in ("user", "test", "system"):
        raise ValueError(f"invalid origin: {event.origin}")
    if event.seq < 0 or event.request_epoch < 0:
        raise ValueError("seq/request_epoch must be non-negative")


class EventFactory:
    """给一个 Run 造事件：seq 单调递增，event_id 唯一，时钟统一取用。"""

    def __init__(self, session_id: str, run_id: str, request_epoch: int,
                 origin: str = "user", *, sequencer: EventSequencer | None = None):
        self._session_id = session_id
        self._run_id = run_id
        self._request_epoch = request_epoch
        self._origin = origin
        # Explicit recovery seeds and independent factories share a Run high-water.
        self._seq = sequencer or EventSequencer()

    def build(
        self, event_type: str, payload: dict, *, producer: str = Producer.RUNTIME,
        channel: str | None = None, attempt_id: str | None = None,
        step_index: int | None = None, monotonic_ns: int | None = None,
        request_epoch: int | None = None,
    ) -> RuntimeEvent:
        seq = self._seq.next(self._run_id)
        event = RuntimeEvent(
            schema_version=SCHEMA_VERSION,
            event_id=f"{self._run_id}-{seq}",
            session_id=self._session_id,
            run_id=self._run_id,
            seq=seq,
            step_index=step_index,
            attempt_id=attempt_id,
            request_epoch=self._request_epoch if request_epoch is None else request_epoch,
            type=event_type,
            channel=channel if channel is not None else _TYPE_CHANNEL.get(event_type, Channel.STATUS),
            producer=producer,
            origin=self._origin,
            timestamp_utc=datetime.now(UTC).isoformat(),
            monotonic_ns=monotonic_ns if monotonic_ns is not None else time.monotonic_ns(),
            payload=payload,
        )
        validate_event(event)
        return event
