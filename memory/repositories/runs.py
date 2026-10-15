"""运行与 Attempt 仓储；保持 Store 方法签名兼容。"""
import json

_TERMINAL_STATES = ("COMPLETED", "INCOMPLETE", "FAILED", "CANCELLED", "INTERRUPTED")


class RunRepository:
    def create_run(self, run_id: str, session_id: str, input_message_id: str, *,
                   config_snapshot: dict, retry_of_run_id: str | None = None) -> None:
        now = self._now()
        with self.transaction():
            self._conn.execute(
                "INSERT INTO runs(run_id, session_id, input_message_id, state, terminal_state,"
                " retry_of_run_id, config_snapshot, created_at, updated_at, owner_id, owner_generation)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, session_id, input_message_id, "QUEUED", None, retry_of_run_id,
                 json.dumps(config_snapshot, ensure_ascii=False), now, now,
                 self._owner.owner_id, self._owner.generation),
            )


    def update_run_state(self, run_id: str, state: str, *, finish_reason: str | None = None) -> None:
        with self.transaction():
            self._conn.execute(
                "UPDATE runs SET state=?, finish_reason=COALESCE(?, finish_reason), updated_at=?"
                " WHERE run_id=?",
                (state, finish_reason, self._now(), run_id),
            )


    def input_message_id_of(self, run_id: str) -> str | None:
        """Vastar runtime component."""
        with self._lock:
            row = self._conn.execute(
                "SELECT input_message_id FROM runs WHERE run_id=?", (run_id,),
            ).fetchone()
        return row["input_message_id"] if row else None


    def finalize_run(self, run_id: str, terminal_state: str, *, result: dict,
                     durability: str, latencies: dict) -> None:
        if terminal_state not in _TERMINAL_STATES:
            raise ValueError(f"非法终态：{terminal_state}")
        with self.transaction():
            self._conn.execute(
                "UPDATE runs SET state=?, terminal_state=?, result_json=?, durability=?,"
                " latencies_json=?, updated_at=? WHERE run_id=?",
                (terminal_state, terminal_state,
                 json.dumps(result, ensure_ascii=False), durability,
                 json.dumps(latencies, ensure_ascii=False), self._now(), run_id),
            )


    def non_terminal_runs(self) -> list[str]:
        with self._lock:   
            rows = self._conn.execute(
                "SELECT run_id FROM runs WHERE terminal_state IS NULL"
            ).fetchall()
        return [r["run_id"] for r in rows]


    def mark_interrupted(self, run_ids: list[str]) -> int:
        """崩溃恢复：未结束 Run 标记 INTERRUPTED，不自动重跑（返回标记数量）。"""
        if not run_ids:
            return 0
        now = self._now()
        placeholders = ",".join("?" * len(run_ids))
        with self.transaction():
            cur = self._conn.execute(
                "UPDATE runs SET state='INTERRUPTED', terminal_state='INTERRUPTED', updated_at=?"
                f" WHERE terminal_state IS NULL AND run_id IN ({placeholders})",
                (now, *run_ids),
            )
            return cur.rowcount

    def recover_abandoned_runs(self) -> int:
        """只在排他归属已确认后恢复；本 owner 的活动 Run 不属于遗留记录。"""
        with self.transaction():
            self._owner.assert_held()
            rows = self._conn.execute(
                "SELECT run_id FROM runs WHERE terminal_state IS NULL AND"
                " (owner_id IS NULL OR owner_id != ? OR owner_generation IS NULL"
                " OR owner_generation != ?)",
                (self._owner.owner_id, self._owner.generation)).fetchall()
            return self.mark_interrupted([r[0] for r in rows])


    def append_attempt(self, attempt_id: str, run_id: str, *, attempt_index: int,
                       provider: str, model: str, request_params: dict,
                       request_hash: str | None) -> None:
        with self.transaction():
            self._conn.execute(
                "INSERT INTO attempts(attempt_id, run_id, attempt_index, provider, model,"
                " request_params, request_hash, started_at) VALUES(?,?,?,?,?,?,?,?)",
                (attempt_id, run_id, attempt_index, provider, model,
                 json.dumps(request_params, ensure_ascii=False), request_hash, self._now()),
            )


    def finish_attempt(self, attempt_id: str, *, usage: dict | None = None,
                       error: dict | None = None, response_frames: list | None = None) -> None:
        with self.transaction():
            self._conn.execute(
                "UPDATE attempts SET usage_json=?, error_json=?, response_frames=?, finished_at=?"
                " WHERE attempt_id=?",
                (json.dumps(usage, ensure_ascii=False) if usage else None,
                 json.dumps(error, ensure_ascii=False) if error else None,
                 json.dumps(response_frames, ensure_ascii=False) if response_frames else None,
                 self._now(), attempt_id),
            )


