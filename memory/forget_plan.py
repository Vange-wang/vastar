"""删除影响闭包：共享来源共同失效，未知谱系摘要按受影响会话保守失效。"""
import json

from memory.forget_journal import token


def source_id(ref):
    return ref.split('/', 1)[1].rsplit('@r', 1)[0] if ref.startswith('history:') and '/' in ref else None


def plan(store, item, artifacts=None):
    db = store._conn
    _, project, scopes, _ = ownership(db)
    items = [dict(r) for r in db.execute('SELECT * FROM memory_items') if own_scope(r['scope'], scopes)]
    versions = [v for r in db.execute('SELECT item_json FROM memory_revisions WHERE item_json IS NOT NULL')
                if own_scope((v := json.loads(r[0]))['scope'], scopes)]
    selected, sources, sessions = dependency_closure(db, item, versions, items)
    from memory.skill_lineage import release_copies
    release_refs, review_sessions = release_copies(db, project, selected)
    sessions |= review_sessions
    affected = [r for r in items if r['id'] in selected]
    runs = [r[0] for r in db.execute('SELECT run_id,session_id FROM runs') if r[1] in sessions]
    tasks = {r[1] for r in db.execute('SELECT session_id,task_id FROM session_tasks') if r[0] in sessions}
    contexts = []
    for row in db.execute('SELECT * FROM context_snapshots'):
        record = json.loads(row['record_json'])
        if row['run_id'] in runs or record.get('session_id') in sessions:
            contexts.append(row['snapshot_id'])
    checkpoints = [dict(r) for r in db.execute('SELECT * FROM checkpoints') if r['task_id'] in tasks]
    needles = selected | sources
    texts = {v['text'] for v in versions if v['id'] in selected} | {r['text'] for r in affected}
    references = release_refs + [r['body'] for r in checkpoints]
    references += [r['record_json'] for r in db.execute('SELECT * FROM history_sources') if r['source_id'] in sources]
    blobs, incomplete = [], []
    for row in db.execute('SELECT * FROM artifacts'):
        meta = dict(row)
        if meta['owner'] != project:
            continue
        linked = any(meta['uri'] in ref or meta['artifact_id'] in ref for ref in references)
        linked |= any(n and n in meta['source_ref'] for n in needles | texts)
        if artifacts is not None:
            try:
                data = artifacts._blob_path(meta).read_bytes().decode('utf-8', errors='replace')
                linked |= any(n and n in data for n in needles | texts)
            except OSError:
                incomplete.append('artifact_unreadable:' + meta['artifact_id'])
        else:
            incomplete.append('artifact_store_unavailable:' + meta['artifact_id'])
        if linked:
            blobs.append({'artifact_id': meta['artifact_id'], 'version': meta['version']})
    audit_rows, unsupported = audit_impact(db, needles | texts)
    return {'items': sorted(selected), 'tokens': sorted(token(r['scope'], r['key']) for r in affected),
            'item_versions': {r['id']: r['revision'] for r in affected},
            'item_scopes': {r['id']: r['scope'] for r in affected},
            'sources': sorted(sources), 'sessions': sorted(sessions), 'runs': runs,
            'contexts': contexts, 'checkpoints': [r['checkpoint_id'] for r in checkpoints],
            'artifacts': blobs, 'audit_rows': audit_rows, 'incomplete': sorted(set(incomplete + unsupported)),
            'policy': '共享来源联动；缺少精确谱系的会话助手/工具输出、摘要、检查点失效；用户其他原文保留。'}


def dependency_closure(db, item, versions, items):
    selected, sources, sessions = {item['id']}, set(), set()
    values = versions + [json.loads(r['record_json']) for r in items]
    _, _, _, authorized = ownership(db)
    history = [r for r in db.execute('SELECT h.*,m.role FROM history_sources h LEFT JOIN messages m ON m.message_id=h.source_id')
               if r['session_id'] in authorized]
    snapshots = [r for r in db.execute('SELECT session_id,memory_snapshot_json FROM sessions') if r[0] in authorized]
    while True:
        before = len(selected), len(sources), len(sessions)
        for value in values:
            refs = {source_id(ref) for ref in value.get('source_refs', [])} - {None}
            citations = {ref.rsplit('@r', 1)[0] for ref in value.get('citations', [])}
            if value['id'] in selected or sources & refs or citations & selected:
                selected.add(value['id'])
                sources |= refs
        needles = selected | sources | {v['text'] for v in values if v['id'] in selected}
        for row in history:
            linked = any(n and (n in row['body'] or n in row['record_json']) for n in needles)
            if row['source_id'] in sources or linked or (row['session_id'] in sessions and row['role'] != 'user'):
                sources.add(row['source_id'])
                sessions.add(row['session_id'])
        for row in snapshots:
            if any(v['id'] in selected for v in json.loads(row[1] or '[]')):
                sessions.add(row[0])
        if before == (len(selected), len(sources), len(sessions)):
            return selected, sources, sessions


def audit_impact(db, needles):
    rows, pending = [], []
    _, project, scopes, sessions = ownership(db)
    runs = {r[0] for r in db.execute('SELECT run_id,session_id FROM runs') if r[1] in sessions}
    sources = {r[0] for r in db.execute('SELECT source_id,session_id FROM history_sources') if r[1] in sessions}
    foreign_items = {r[0] for r in db.execute('SELECT id,scope FROM memory_items') if not own_scope(r[1], scopes)}
    # 内容载体使用固定白名单；只保表/rowid/列元数据到外部撤回日志。
    surfaces = {'tool_operations': ('facts', 'receipt', 'reconcile_result', 'resource_version'),
                'events': ('payload',), 'attempts': ('request_params', 'error_json', 'response_frames'),
                'history_versions': ('body', 'record_json'), 'memory_revisions': ('value', 'item_json'),
                'history_model_calls': ('record_json',),
                'agent_proposals': ('record_json', 'receipt_json'),
                'memory_conflicts': ('record_json',)}
    for table, columns in surfaces.items():
        for row in db.execute(f'SELECT rowid AS _rid,* FROM {table}'):
            if ('session_id' in row.keys() and row['session_id'] not in sessions
                    or 'run_id' in row.keys() and row['run_id'] not in runs
                    or 'scope' in row.keys() and not own_scope(row['scope'], scopes)
                    or table == 'history_versions' and row['source_id'] not in sources
                    or table == 'memory_revisions' and row['key'] in foreign_items):
                continue
            matches = [c for c in columns if isinstance(row[c], str) and any(n and n in row[c] for n in needles)]
            if matches:
                rows.append({'table': table, 'rowid': row['_rid'], 'columns': matches})
    # Task acceptance 属用户冻结目标，删除记忆不能悄悄改验收项；显式列为待用户处理。
    for row in db.execute('SELECT * FROM tasks'):
        if row['project_id'] != project:
            continue
        if any(n and n in str(row[c]) for n in needles for c in ('goal', 'source_ref', 'constraints', 'acceptance', 'clarifications')):
            pending.append('task_definition_requires_user_edit:' + row['task_id'])
    return rows, pending


def ownership(db):
    row = db.execute('SELECT user_id,project_id FROM history_identity WHERE id=1').fetchone()
    if row is None:
        raise ValueError('forget_requires_bound_identity')
    user, project = row
    scopes = {'user:' + user, 'project:' + project}
    scopes |= {'task:' + r[0] for r in db.execute('SELECT task_id FROM tasks WHERE project_id=?', (project,))}
    sessions = {r[0] for r in db.execute('SELECT session_id FROM history_scopes WHERE user_id=? AND project_id=?', (user, project))}
    return user, project, scopes, sessions


def own_scope(scope, scopes):
    # Agent 域属于已绑定库的同一用户；直接选择其他Agent仍由scope_for拒绝。
    return scope in scopes or scope.startswith('agent:')
