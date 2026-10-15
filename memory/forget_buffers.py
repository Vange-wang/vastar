"""撤回后的 writer 迟到事件先脱敏再判幂等；保留 usage/状态，不复活旧正文。"""
import json
from dataclasses import replace


def entries_for_run(store, run_id):
    return [e for row in store._conn.execute('SELECT record_json FROM memory_actions')
            if run_id in (e := json.loads(row[0]))['impact']['runs']]


def sanitize_event(store, event):
    if not entries_for_run(store, event.run_id):
        return event
    return replace(event, payload=safe_payload(event.type, event.channel, event.payload))


def safe_payload(kind, channel, body):
    if kind == 'history.tool_exchange':
        return {'messages': [], 'pair_status': 'incomplete', 'notes': ['memory_revoked']}
    if channel in ('metrics', 'state'):
        return body
    keep = {'from', 'to', 'status', 'partial', 'answer_message_id', 'attempt_index',
            'provider', 'model', 'latencies', 'tool', 'name'}
    return {**{k: v for k, v in body.items() if k in keep}, 'redacted': True}


def invalidate_projector(projector, run_id):
    projector._content.pop(run_id, None)
    projector._reasoning.pop(run_id, None)
    projector._meta.pop(run_id, None)


def clean_after_batch(store, projector, runs):
    from memory.forget_cleanup import apply_entry
    for rid in runs:
        entries = entries_for_run(store, rid)
        for entry in entries:
            apply_entry(store, entry, repeat=True)
        if entries:
            invalidate_projector(projector, rid)


def clear_caches(store, entry):
    clear_knowledge = getattr(store, '_knowledge_clear', None)
    if clear_knowledge is not None:
        clear_knowledge()
    for projector in getattr(store, '_memory_caches', ()):
        for rid in entry['impact']['runs']:
            invalidate_projector(projector, rid)
