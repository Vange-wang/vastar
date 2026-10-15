"""Exact logical resource leases, shared by workers; unresolved durable intents fence retries."""
import threading
from contextlib import ExitStack

from core.tools.browser_protocol import BrowserFailure

_LOCK = threading.RLock()
_ACTIVE = {}
_PROCESS_LOCKS = {}


def keys(session, tab_id, config):
    tab = session.tab(tab_id)
    origin, url = tab['origin'], tab['url']
    document = config.get('document_keys', {}).get(url, url.split('#')[0].split('?')[0])
    return sorted({f'tab:{session.browser_session_id}:{tab_id}', f'login:{origin}', f'document:{document}'})


def claim(store, owner, resources, *, reconciling=None, lock_root=None):
    resources = frozenset(resources)
    with _LOCK, store._lock:
        if owner in _ACTIVE or any(resources & held for held in _ACTIVE.values()):
            raise BrowserFailure('resource_busy')
        stack = ExitStack()
        try:
            if lock_root is not None:
                from core.orchestration.resource_locks import ResourceLock
                locks = ResourceLock(lock_root)
                for key in sorted(resources):
                    stack.enter_context(locks.hold(key, owner))
            rows = store._conn.execute("SELECT operation_id,facts FROM tool_operations WHERE tool IN "
                "('browser_click','browser_fill') AND state IN ('prepared','executing','unknown')").fetchall()
            import json
            for row in rows:
                if row[0] != reconciling and resources.intersection(json.loads(row[1]).get('resource_keys', [])):
                    raise BrowserFailure('resource_unresolved')
            _ACTIVE[owner] = resources
            _PROCESS_LOCKS[owner] = stack
        except ValueError as exc:
            stack.close()
            raise BrowserFailure('resource_busy') from exc
        except BaseException:
            stack.close()
            raise


def release(owner):
    with _LOCK:
        _ACTIVE.pop(owner, None)
        stack = _PROCESS_LOCKS.pop(owner, None)
        if stack is not None:
            stack.close()
