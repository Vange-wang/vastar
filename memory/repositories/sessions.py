"""Read-only session catalogue and complete, stable message replay."""

LIST_SQL = """SELECT s.*,
 (SELECT count(*) FROM messages m WHERE m.session_id=s.session_id) AS message_count,
 (SELECT content FROM messages m WHERE m.session_id=s.session_id AND role='user'
  ORDER BY created_at, rowid LIMIT 1) AS first_user
 FROM (SELECT * FROM sessions ORDER BY created_at DESC, session_id DESC LIMIT ?) s
 ORDER BY s.created_at DESC, s.session_id DESC"""


class SessionRepository:
    def list_sessions(self, limit=10):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError('会话数须为 1–50')
        with self._lock:
            return [dict(r) for r in self._conn.execute(LIST_SQL, (limit,))]

    def load_session(self, session_id):
        with self._lock:
            row = self._conn.execute('SELECT * FROM sessions WHERE session_id=?', (session_id,)).fetchone()
            if row is None:
                raise ValueError('会话不存在，请重新 /sessions')
            messages = [dict(r) for r in self._conn.execute(
                'SELECT * FROM messages WHERE session_id=? ORDER BY created_at, rowid', (session_id,))]
            return dict(row), messages
