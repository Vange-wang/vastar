"""Explicit session selection; never carry pending inputs across sessions."""
import itertools


class SessionResumeMixin:
    def list_sessions(self, limit=10):
        return self._store.list_sessions(limit)

    async def resume_session(self, session_id, *, page=None):
        if self.is_busy() or self.has_draft_queue():
            raise ValueError('正在回答或有排队输入，请处理完成后再切换会话')
        if await self._writer.flush() != 'confirmed':
            raise ValueError('消息尚未保存，暂不能切换会话')
        row, messages = self._store.load_session(session_id)
        if page is not None and not 1 <= page <= max(1, (len(messages) + 19) // 20):
            raise ValueError('页码越界，当前会话未改变')
        self.disarm_skill()
        self._runtime.rotate_ask_session(session_id)
        self.session_id = session_id
        self.mode = row['mode']
        self._seq = itertools.count(max((m['submit_seq'] or 0 for m in messages), default=0) + 1)
        self._last_user_input = next((m['content'] for m in reversed(messages) if m['role'] == 'user'), None)
        self.last_result = None
        self.paused = False
        self._pause_after_interrupt = False
        self._cancel_pending = False
        self.ensure_session()
        self.paused = bool(self._pending)
        if self._pending:
            self._seq = itertools.count(max([m['submit_seq'] or 0 for m in messages]
                                           + [p.submit_seq for p in self._pending]) + 1)
        from core.memory_session import freeze_memory
        freeze_memory(self)
        return messages
