"""来源核实：只接受可回读的授权历史原文，归因与权威均从库内事实取得。"""
import json
import re

from memory.retrieval import HistoryAccess, HistoryService


def source_text(content):
    if content.startswith('/remember '):
        body = content[len('/remember '):].strip()
        if body.startswith('{'):
            return json.loads(body)['text']
        return body.partition('=')[2].strip()
    return re.sub(r'^\s*(?:请)?记住[：:，,\s]+', '', content).strip()


def verify_sources(service, session_id, proposal):
    from memory.knowledge_codec import verify_bound_source
    verify_bound_source(service, proposal)
    refs = proposal.get('source_refs', [])
    if not isinstance(refs, list) or not 1 <= len(refs) <= 16:
        raise ValueError('source_unknown')
    sources = []
    history = HistoryService(service.store, HistoryAccess(service.user_id, service.project_id))
    for ref in refs:
        match = re.fullmatch(r'history:([^/]+)/([^/]+)@r(\d+)', ref)
        if not match:
            raise ValueError('source_unknown')
        sid, mid, revision = match.groups()
        page = history.list(source_ids=[mid])
        rows = page['items']
        if not rows or rows[0]['session_id'] != sid or rows[0]['revision'] != int(revision):
            raise ValueError('source_missing_or_changed')
        row = service.store._conn.execute('SELECT * FROM messages WHERE message_id=?', (mid,)).fetchone()
        if row is None or proposal['text'] != source_text(row['content']):
            raise ValueError('source_does_not_support_text')
        if proposal['kind'] != 'hypothesis' and row['role'] != 'user':
            raise ValueError('model_is_not_user_fact')
        sources.append(dict(row))
    return sources


def explicit_source(sources, session_id, message_id):
    for row in sources:
        if row['session_id'] == session_id and row['message_id'] == message_id and row['role'] == 'user':
            if row['content'].startswith('/remember ') or re.match(r'^\s*(?:请)?记住[：:，,\s]', row['content']):
                return True
    return False


def explicit_scope(sources, message_id, default):
    for row in sources:
        if row['message_id'] == message_id and row['content'].startswith('/remember {'):
            return json.loads(row['content'][len('/remember '):]).get('scope', default)
    return default


def explicit_replace(sources, message_id, proposal):
    for row in sources:
        if row['message_id'] == message_id and row['content'].startswith('/remember {'):
            approved = json.loads(row['content'][len('/remember '):])
            return (approved.get('replace') is True and all(approved.get(k) == proposal.get(k)
                    for k in ('key', 'kind', 'text', 'expected_revision', 'scope', 'validity')))
    return False
