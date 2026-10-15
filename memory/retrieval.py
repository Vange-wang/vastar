"""T21 首版只搜历史。范围权限先判，候选先限界，分数不是事实概率。"""
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from memory.history_index import match_rows

MAX_CANDIDATES = 200


@dataclass(frozen=True)
class HistoryAccess:
    user_id: str
    project_id: str
    source_ids: tuple[str, ...] | None = None
    since: str | None = None
    until: str | None = None


def utc_time(value):
    if value is None:
        return None
    date = datetime.fromisoformat(value)
    if date.tzinfo is None:
        raise ValueError('时间须含时区；范围为 [since, until)')
    return date.astimezone(UTC).isoformat()


def authorize(access, user_id, project_id, source_ids, since, until):
    if user_id != access.user_id or project_id != access.project_id:
        raise PermissionError('历史检索拒绝跨 user/project')
    if source_ids is not None and not isinstance(source_ids, (list, tuple)):
        raise ValueError('source_ids 必须为 ID 数组')
    sources = tuple(source_ids) if source_ids is not None else access.source_ids
    if access.source_ids is not None and (sources is None or not set(sources) <= set(access.source_ids)):
        raise PermissionError('历史检索拒绝越 source')
    start, end = utc_time(since or access.since), utc_time(until or access.until)
    if access.since and (start is None or start < utc_time(access.since)):
        raise PermissionError('历史检索拒绝越时间范围')
    if access.until and (end is None or end > utc_time(access.until)):
        raise PermissionError('历史检索拒绝越时间范围')
    if start and end and start >= end:
        raise ValueError('since 必须早于 until')
    return sources, start, end


def candidate_query(access, sources, since, until, after, limit):
    clauses = ["s.user_id=?", "s.project_id=?", "h.status='active'", 'h.id>?']
    args = [access.user_id, access.project_id, after]
    if sources is not None:
        clauses.append('h.source_id IN (' + ','.join('?' for _ in sources) + ')')
        args.extend(sources)
    if since:
        clauses.append('h.created_at>=?')
        args.append(since)
    if until:
        clauses.append('h.created_at<?')
        args.append(until)
    # 删除原消息/会话即失效；残留 FTS 绝不充当原证据。
    clauses.append("EXISTS(SELECT 1 FROM sessions x WHERE x.session_id=h.session_id)")
    clauses.append("(h.kind!='message' OR EXISTS(SELECT 1 FROM messages m WHERE m.message_id=h.source_id))")
    return ('SELECT h.* FROM history_scopes s JOIN history_sources h ON h.session_id=s.session_id WHERE '
            + ' AND '.join(clauses) + ' ORDER BY h.id LIMIT ?', [*args, limit + 1])


class HistoryService:
    def __init__(self, store, access, artifacts=None):
        self.store, self.access = store, access
        self.artifacts = artifacts

    def list(self, *, user_id=None, project_id=None, source_ids=None, since=None, until=None,
             after=0, limit=50):
        try:
            from memory.expiry import expire_contexts
            from memory.forget_journal import ensure_replayed
            sources, start, end = authorize(self.access, user_id or self.access.user_id,
                                            project_id or self.access.project_id, source_ids, since, until)
            if isinstance(after, bool) or not isinstance(after, int) or after < 0 or not 1 <= limit <= MAX_CANDIDATES:
                raise ValueError('分页参数：after >=0；limit 1..200')
            if sources is not None and (len(sources) > MAX_CANDIDATES or any(not isinstance(s, str) for s in sources)):
                raise ValueError('source_ids 必须为最多 200 个 ID')
            ensure_replayed(self.store)
            expire_contexts(self.store)
            sql, args = candidate_query(self.access, sources, start, end, after, limit)
            with self.store._lock:
                rows = [dict(r) for r in self.store._conn.execute(sql, args)]
            return {'status': 'ok' if rows else 'empty', 'items': rows[:limit],
                    'next_cursor': rows[limit - 1]['id'] if len(rows) > limit else None,
                    'scope': {'user_id': self.access.user_id, 'project_id': self.access.project_id},
                    'time_semantics': '[since,until)'}
        except PermissionError as exc:
            return {'status': 'denied', 'items': [], 'reason': str(exc), 'next_cursor': None}
        except (sqlite3.Error, AttributeError, ValueError, TypeError) as exc:
            return {'status': 'unavailable', 'items': [], 'reason': type(exc).__name__, 'next_cursor': None}

    def search(self, query, *, after=0, limit=20, **scope):
        if not isinstance(query, str) or not query.strip() or len(query) > 256:
            return {'status': 'unavailable', 'items': [], 'reason': 'query 必须为 1..256 字符的非空文本'}
        # 整个候选窗口与匹配同一锁内，撤回/删除不能在其间穿插。
        with self.store._lock:
            page = self.list(after=after, limit=limit, **scope)
            if page['status'] not in ('ok', 'empty'):
                return page
            candidates, invalid = readable_sources(page['items'], self.artifacts)
            rows, strategy = match_rows(self.store, candidates, query)
            return {**page, 'status': 'ok' if rows else 'unavailable' if invalid else 'empty',
                    'items': [result_record(row, query, strategy) for row in rows],
                    'strategy': strategy, 'scanned': len(page['items']),
                    'invalid_sources': invalid,
                    'complete': page['next_cursor'] is None,
                    'note': '分数仅表示本页文字匹配，不是事实概率；有 next_cursor 须继续查后页。'}


def result_record(row, query, strategy):
    pos = row['body'].casefold().find(query.casefold())
    start = max(0, pos - 60)
    return {'source_id': row['source_id'], 'revision': row['revision'], 'time': row['created_at'],
            'snippet': row['body'][start:start + 240], 'score': 1.0, 'score_kind': strategy,
            'reference': f"history:{row['session_id']}/{row['source_id']}@r{row['revision']}",
            'kind': row['kind'], 'run_id': row['run_id'],
            'provenance': json.loads(row['record_json']) if row['kind'] == 'tool_exchange' else {}}


def readable_sources(rows, artifacts):
    from core.artifacts import ArtifactError
    valid, invalid = [], []
    for row in rows:
        try:
            for ref in json.loads(row['record_json']).get('artifact_refs', []):
                if artifacts is None:
                    raise ArtifactError('产物库未装配')
                aid, version, scope = artifacts._parse_ref(ref)
                meta, _ = artifacts.get(aid, version)
                if meta['uri'] != ref or meta['scope'] != scope:
                    raise ArtifactError('历史产物引用身份不符')
            valid.append(row)
        except (ArtifactError, OSError, ValueError):
            invalid.append({'source_id': row['source_id'], 'reason': 'source_unavailable'})
    return valid, invalid
