"""持久输入与检查点查询。"""
import json

from memory.repositories.tasks import encode


class CheckpointRepository:
    def checkpoint_latest(self, task_id):
        with self._lock:
            row = self._conn.execute("SELECT * FROM checkpoints WHERE task_id=? AND state='committed'"
                                     " ORDER BY rowid DESC LIMIT 1", (task_id,)).fetchone()
            return dict(row) if row else None

    def pending_save(self, session_id, body):
        with self.transaction():
            self._conn.execute("INSERT INTO pending_inputs VALUES(?,?,?,?,?,?)",
                               (body["message_id"] + ":" + body["run_id"], session_id,
                                body["run_id"], encode(body), "queued", self._now()))

    def pending_state(self, run_id, state):
        with self.transaction():
            self._conn.execute("UPDATE pending_inputs SET state=? WHERE run_id=?", (state, run_id))

    def pending_list(self, session_id, state="queued"):
        with self._lock:
            return [json.loads(row[0]) for row in self._conn.execute(
                "SELECT body FROM pending_inputs WHERE session_id=? AND state=? ORDER BY rowid",
                (session_id, state))]
