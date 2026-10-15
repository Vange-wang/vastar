"""代次守卫封锁在途 Run 的旧上下文；缓存失效后必须从受管存储重新装配。"""
from core.errors import ErrorCategory, ErrorCode, VastarError


def generation_guard(store, session_id):
    def generation():
        refresh = getattr(store, '_knowledge_guard', None)
        if refresh is not None:
            refresh()
        with store._lock:
            from memory.forget_journal import ensure_replayed
            ensure_replayed(store)
            from memory.expiry import expire_contexts
            expire_contexts(store)
            row = store._conn.execute('SELECT context_generation FROM sessions WHERE session_id=?', (session_id,)).fetchone()
            return row[0] if row else None
    initial = generation()

    def check():
        if generation() != initial:
            raise VastarError(ErrorCode.PARAM_ERROR, ErrorCategory.PARAM,
                              '记忆已撤回/回滚，旧 Run 上下文缓存失效；请重新提交以重建上下文。')
    return check


def check_request(request):
    guard = getattr(request, 'memory_guard', None)
    if guard is not None:
        try:
            guard()
        except VastarError:
            request.history.clear()
            request.approved_compact = None
            request.cancel_event.set()
            raise
