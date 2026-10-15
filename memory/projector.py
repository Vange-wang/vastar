"""事务内事件投影；缓存仅在事务成功后生效。"""

from core.events import EventType, RuntimeEvent
from memory.store import Store


class EventProjector:
    """事件流 → runs/attempts/messages 关系行投影（存储门面侧，不改事件本身）。"""

    def __init__(self, store: Store) -> None:
        self._store = store
        import weakref
        if not hasattr(store, '_memory_caches'):
            store._memory_caches = weakref.WeakSet()
        store._memory_caches.add(self)
        self._answer_ids: dict[str, str] = {}          # run_id -> assistant message_id
        self._content: dict[str, str] = {}             # run_id -> 正文累计
        self._reasoning: dict[str, str] = {}           # run_id -> 思考累计（分字段）
        self._open_attempt: dict[str, str] = {}        # run_id -> attempt_id（未结束）
        self._attempt_usage: dict[str, dict] = {}      # attempt_id -> usage
        self._attempt_error: dict[str, dict] = {}      # attempt_id -> error
        self._meta: dict[str, dict] = {}               # run_id -> latencies/context/status/partial

    # ---- 协调器入口 ------------------------------------------------------------

    def set_answer_message_id(self, run_id: str, message_id: str) -> None:
        self._answer_ids[run_id] = message_id

    # ---- 事件处理 ---------------------------------------------------------------

    def process(self, event: RuntimeEvent) -> None:
        self._store.record_history_event(event)
        t = event.type
        if t == EventType.ATTEMPT_STARTED:
            self._close_open_attempt(event.run_id)
            p = event.payload
            self._store.append_attempt(
                event.attempt_id, event.run_id,
                attempt_index=int(p.get("attempt_index", 0)),
                provider=str(p.get("provider", "")), model=str(p.get("model", "")),
                request_params=dict(p.get("request_params", {})),
                request_hash=p.get("request_hash"),
            )
            self._open_attempt[event.run_id] = event.attempt_id
        elif t in (EventType.CONTENT_DELTA, EventType.REASONING_DELTA):
            text = str(event.payload.get("text", ""))
            if t == EventType.CONTENT_DELTA:
                self._content[event.run_id] = self._content.get(event.run_id, "") + text
            else:
                self._reasoning[event.run_id] = self._reasoning.get(event.run_id, "") + text
        elif t == EventType.USAGE_REPORTED:
            if event.attempt_id:
                self._attempt_usage[event.attempt_id] = dict(event.payload)
        elif t == EventType.ERROR:
            key = event.attempt_id or f"run:{event.run_id}"
            self._attempt_error[key] = dict(event.payload)
        elif t == EventType.RUN_STATE_CHANGED:
            self._store.update_run_state(event.run_id, str(event.payload.get("to", "")))
        elif t == EventType.RUN_ENDED:
            self._on_run_ended(event)

    def _on_run_ended(self, event: RuntimeEvent) -> None:
        run_id = event.run_id
        p = event.payload
        self._close_open_attempt(run_id, frames=p.get("frames_by_attempt") or {})
        self._meta[run_id] = {
            "latencies": dict(p.get("latencies", {})),
            "context_record": p.get("context_snapshot"),
            "status": str(p.get("status", "")),
            "partial": bool(p.get("partial", False)),
        }
        message_id = self._answer_ids.get(run_id) or p.get("answer_message_id")
        if message_id:
            status = "complete" if str(p.get("status")) == "completed" and not p.get("partial") else "partial"
            self._store.append_message(
                message_id, event.session_id, role="assistant",
                content=self._content.get(run_id, ""),
                reasoning_content=self._reasoning.get(run_id, ""),
                status=status, origin=event.origin, run_id=run_id,
            )
        if p.get("result") is not None:
            result = {**p["result"], "durability": "confirmed", "last_durable_seq": event.seq}
            self._store.finalize_run(run_id, str(p["status"]).upper(), result=result,
                                     durability="confirmed", latencies=p.get("latencies", {}))
        if p.get("context_snapshot") is not None:
            self._store.save_context_snapshot(f"{run_id}-ctx", run_id, p["context_snapshot"])
        self._content.pop(run_id, None)
        self._reasoning.pop(run_id, None)
        
        # ① _answer_ids（答案已落地）；② _meta 只保留最近一个 Run（读取方语义 =
        # "最近一次 Run" 的 latencies/context 快照，见 latency_of/context_record_of）；
        # ③ _attempt_error 的 run:{id} 回退键（无 attempt_id 的 Run 级错误，此处为其唯一清理点）。
        self._answer_ids.pop(run_id, None)
        self._meta = {run_id: self._meta.get(run_id, {})} if run_id in self._meta else {}
        self._attempt_error.pop(f"run:{run_id}", None)

    def _close_open_attempt(self, run_id: str, frames: dict | None = None) -> None:
        attempt_id = self._open_attempt.pop(run_id, None)
        if attempt_id is None:
            return
        self._store.finish_attempt(
            attempt_id,
            usage=self._attempt_usage.pop(attempt_id, None),
            error=self._attempt_error.pop(attempt_id, None),
            response_frames=(frames or {}).get(attempt_id),
        )

    def flush_partial_message(self, run_id: str) -> None:
        """批次刷盘时同步 upsert 半成品消息行（崩溃可恢复已接收内容）。"""
        message_id = self._answer_ids.get(run_id)
        if message_id is None or (run_id not in self._content and run_id not in self._reasoning):
            return
        self._store.upsert_partial_assistant(
            message_id=message_id, run_id=run_id,
            content=self._content.get(run_id, ""),
            reasoning_content=self._reasoning.get(run_id, ""),
        )


