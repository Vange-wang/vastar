"""事件与投影原子落盘；默认 32 条或 250ms 触发批次。

250ms 是调度目标而非硬实时保证，受事件循环和磁盘延迟影响。
未确认的 buffered/failed/inflight 事件在进程崩溃时可能丢失。
关键操作必须 await commit；关停必须 await close 后再关 Store。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from copy import deepcopy

from core.errors import ErrorCategory, ErrorCode, VastarError
from core.events import RuntimeEvent
from memory.projector import EventProjector
from memory.store import Store
from memory.writer_lifecycle import WriterLifecycle

logger = logging.getLogger("vastar.store")
BATCH_SIZE = 32
FLUSH_INTERVAL_S = 0.25
DURABILITY_CONFIRMED = "confirmed"
DURABILITY_PARTIAL = "partial"
DURABILITY_NONE = "none"


class Origin:
    """正式入口 user/system；test 仅用于隔离测试。"""
    USER = "user"
    SYSTEM = "system"
    TEST = "test"


class StoreWriter(WriterLifecycle):
    """单写入者 FIFO；健康表示最近写入结果，durability 表示确认水位。"""

    def __init__(self, store: Store, *, batch_size: int = BATCH_SIZE,
                 flush_interval_s: float = FLUSH_INTERVAL_S,
                 clock: Callable[[], float] | None = None) -> None:
        if batch_size < 1 or flush_interval_s <= 0:
            raise ValueError("batch_size 与 flush_interval_s 必须为正")
        self._store = store
        self.projector = EventProjector(store)
        self._batch_size = batch_size
        self._flush_interval = flush_interval_s
        self._clock = clock or time.monotonic
        self._buffer: list[RuntimeEvent] = []
        self._pending_unsaved: list[RuntimeEvent] = []
        self._inflight: list[RuntimeEvent] = []
        self._committed = 0
        self._accepted = 0
        self._failed = False
        self._last_error: Exception | None = None
        self._last_flush = self._clock()
        self._write_lock = asyncio.Lock()
        self._durable_seq: dict[str, int] = {}
        self._timer = None
        self._timer_task = None
        self._closing = False
        self._close_task = None

    @property
    def last_error(self) -> Exception | None:
        return self._last_error

    def unsaved_count(self) -> int:
        return len(self._pending_unsaved) + len(self._buffer) + len(self._inflight)

    @property
    def durability(self) -> str:
        if self.unsaved_count() == 0:
            return DURABILITY_CONFIRMED
        return DURABILITY_PARTIAL if self._committed else DURABILITY_NONE

    def storage_status(self) -> dict:
        return {"health": "healthy" if self.last_error is None else "degraded",
                "pending_count": self.unsaved_count(), "durability": self.durability,
                "accepted_watermark": self._accepted, "confirmed_watermark": self._committed}

    def set_answer_message_id(self, run_id: str, message_id: str) -> None:
        with self._store._lock:
            self.projector.set_answer_message_id(run_id, message_id)

    def last_durable_seq(self, run_id: str) -> int | None:
        return self._durable_seq.get(run_id)

    def last_latencies(self, run_id: str) -> dict:
        return self.projector._meta.get(run_id, {}).get("latencies", {})

    def last_context_record(self, run_id: str) -> dict | None:
        return self.projector._meta.get(run_id, {}).get("context_record")

    def storage_error(self) -> VastarError:
        return VastarError(
            code=ErrorCode.STORE_WRITE_FAILED, category=ErrorCategory.INTERNAL,
            message=f"会话存储写入失败或尚未确认：{self._last_error}",
            details={"unsaved_events": self.unsaved_count(), **self.storage_status()},
        )

    async def append(self, event: RuntimeEvent) -> None:
        if self._closing:
            raise RuntimeError("StoreWriter 已关闭，不能接收新事件")
        self._buffer.append(deepcopy(event))
        self._accepted += 1
        self._arm_timer()
        if (len(self._buffer) >= self._batch_size
                or self._clock() - self._last_flush >= self._flush_interval):
            await self.flush_batch()

    async def flush_batch(self) -> None:
        """锁内取批次；失败头部保留，后续事件不得越过失败批次。"""
        async with self._write_lock:
            self._cancel_timer()
            queued = self._pending_unsaved + self._buffer
            self._pending_unsaved = []
            self._buffer = []
            for offset in range(0, len(queued), self._batch_size):
                batch = queued[offset:offset + self._batch_size]
                # 未开始的尾部也计入待确认水位。
                self._pending_unsaved = queued[offset + len(batch):]
                self._inflight = batch
                cancelled = await self._write_batch(batch)
                if self._failed:
                    self._pending_unsaved = batch + self._pending_unsaved
                self._inflight = []
                if cancelled:
                    raise asyncio.CancelledError
                if self._failed:
                    break
            self._last_flush = self._clock()
            self._arm_timer()

    async def _write_batch(self, batch) -> bool:
        job = asyncio.create_task(asyncio.to_thread(self._commit, batch))
        cancelled = False
        # 取消 await 不会停止 sqlite 工作线程，必须等线程结束再释放写锁。
        while not job.done():
            try:
                await asyncio.shield(job)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
        try:
            job.result()
        except Exception as exc:
            self._failed = True
            self._last_error = exc
            logger.error("会话存储写入失败：%s；未确认 %d 条", exc, self.unsaved_count())
        else:
            self._committed += len(batch)
            self._last_error = None
            self._failed = False
            for event in batch:
                self._durable_seq[event.run_id] = max(
                    self._durable_seq.get(event.run_id, -1), event.seq)
        return cancelled

    def _commit(self, batch: list[RuntimeEvent]) -> None:
        self._store.append_batch(batch, self.projector)

    async def flush(self) -> str:
        """显式刷盘，失败最多补试一次；不无限重试永久错误。"""
        await self.flush_batch()
        if self._failed:
            await self.flush_batch()
        return self.durability
