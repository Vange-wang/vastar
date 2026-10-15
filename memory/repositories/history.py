"""历史身份、完整工具配对与调用来源仓储；Store 仅混入。"""
import json


class HistoryRepository:
    def bind_history_scope(self, user_id, project_id):
        if not user_id or not project_id:
            raise ValueError('历史 scope 必须有 user/project')
        with self.transaction():
            old = self._conn.execute('SELECT user_id,project_id FROM history_identity WHERE id=1').fetchone()
            if old is not None and tuple(old) != (user_id, project_id):
                raise ValueError('历史库已绑定其他 user/project；拒绝重新归属')
            self._conn.execute('INSERT OR IGNORE INTO history_identity VALUES(1,?,?)', (user_id, project_id))
            self._conn.execute('INSERT OR IGNORE INTO history_scopes SELECT session_id,?,? FROM sessions',
                               (user_id, project_id))

    def history_count(self, session_id):
        with self._lock:
            return self._conn.execute('SELECT count(*) FROM messages WHERE session_id=?', (session_id,)).fetchone()[0]

    def record_history_event(self, event):
        p = event.payload
        if event.type in ('model_call.intent', 'model_call.finished'):
            self._conn.execute('INSERT INTO history_model_calls VALUES(?,?,?) ON CONFLICT(call_id) '
                               'DO UPDATE SET record_json=excluded.record_json',
                               (p['call_id'], event.run_id, json.dumps(p, ensure_ascii=False)))
        elif event.type == 'history.tool_exchange':
            self._conn.execute('INSERT OR IGNORE INTO history_sources'
                               '(source_id,session_id,run_id,kind,body,record_json,created_at) VALUES(?,?,?,?,?,?,?)',
                               (event.event_id, event.session_id, event.run_id, 'tool_exchange',
                                json.dumps(p['messages'], ensure_ascii=False), json.dumps(p, ensure_ascii=False),
                                event.timestamp_utc))

    def history_page(self, session_id, *, after=0, limit=50):
        from memory.forget_journal import ensure_replayed
        ensure_replayed(self)
        from memory.expiry import expire_contexts
        expire_contexts(self)
        if not 1 <= limit <= 200 or after < 0:
            raise ValueError('history page: limit 1..200, after >=0')
        with self._lock:
            rows = self._conn.execute('SELECT * FROM history_sources WHERE session_id=? '
                                      "AND id>? AND status='active' ORDER BY id LIMIT ?",
                                      (session_id, after, limit + 1)).fetchall()
            items = []
            for raw in rows[:limit]:
                row = dict(raw)
                row['record'] = json.loads(row.pop('record_json'))
                row['model_calls'] = [json.loads(r[0]) for r in self._conn.execute(
                    'SELECT record_json FROM history_model_calls WHERE run_id=?', (row['run_id'],))]
                items.append(row)
            return {'items': items, 'next_cursor': rows[limit - 1]['id'] if len(rows) > limit else None}

    def history_messages(self, session_id):
        from memory.history import context_history
        with self._lock:
            return context_history(self, session_id)
