"""事件仓储：事件身份校验与幂等批量写入。"""

import json
from contextlib import contextmanager
from copy import deepcopy

from core.events import partition_fresh_events


def _event_row(e: dict) -> tuple:
    """Vastar runtime component."""
    return (e["event_id"], e["session_id"], e["run_id"], e["seq"], e["request_epoch"],
            e["type"], e["channel"], e["producer"], e["origin"], e["timestamp_utc"],
            e["monotonic_ns"], json.dumps(e["payload"], ensure_ascii=False))


class EventStoreMixin:
    @contextmanager
    def transaction(self):
        """重入写事务：内层不得提前提交；异常整批回滚。"""
        with self._lock:
            self._owner.assert_held()
            outer = not self._conn.in_transaction
            if outer:
                self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                if outer:
                    self._conn.commit()
            except BaseException:
                if outer:
                    self._conn.rollback()
                raise

    def _fresh_events(self, events):
        existing = {}
        # 分块避免超过 SQLite 参数上限；外层事务覆盖查重与写入。
        ids = list(dict.fromkeys(e["event_id"] for e in events))
        for offset in range(0, len(ids), 500):
            chunk = ids[offset:offset + 500]
            marks = ",".join("?" * len(chunk))
            existing.update({r["event_id"]: dict(r) for r in self._conn.execute(
                f"SELECT * FROM events WHERE event_id IN ({marks})", chunk)})
        return partition_fresh_events(events, existing)

    def append_batch(self, events, projector):
        """事件与新事件的确定投影原子提交；缓存随回滚恢复。"""
        with self._lock:
            snapshot = deepcopy({k: v for k, v in vars(projector).items() if k != "_store"})
            try:
                with self.transaction():
                    from memory.forget_buffers import clean_after_batch, sanitize_event
                    events = [sanitize_event(self, event) for event in events]
                    records = [event.to_dict() for event in events]
                    fresh = {e["event_id"] for e in self._fresh_events(records)}
                    self.append_events(records)
                    runs = set()
                    for event in events:
                        if event.event_id in fresh:
                            projector.process(event)
                            fresh.remove(event.event_id)
                            runs.add(event.run_id)
                    for run_id in runs:
                        projector.flush_partial_message(run_id)
                    clean_after_batch(self, projector, runs)
            except BaseException:
                for key, value in snapshot.items():
                    setattr(projector, key, value)
                raise

    def append_events(self, events: list[dict]) -> None:
        """Vastar runtime component."""
        if not events:
            return
        with self.transaction():
            fresh = self._fresh_events(events)
            self._conn.executemany(
                "INSERT INTO events(event_id, session_id, run_id, seq, request_epoch,"
                " type, channel, producer, origin, timestamp_utc, monotonic_ns, payload)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                [_event_row(e) for e in fresh],
            )

    def events_page(self, run_id, *, after_seq=0, limit=200, inclusive=False):
        """Vastar runtime component."""
        op = ">=" if inclusive else ">"
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM events WHERE run_id=? AND seq{op}? ORDER BY seq LIMIT ?",
                (run_id, after_seq, limit)).fetchall()
        result = []
        for row in rows:
            event = dict(row)
            event["payload"] = json.loads(event["payload"])
            result.append(event)
        return result
