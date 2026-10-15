"""C07：只从权威仓储投影任务事实；摘要器无权创建或修改这些字段。"""
import hashlib
import json
import re


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encode(value).encode('utf-8')).hexdigest()


def event_range(store, session_id, limit=None):
    rows = store._conn.execute(
        'SELECT rowid,event_id,payload FROM events WHERE session_id=? AND rowid<=? ORDER BY rowid',
        (session_id, limit if limit is not None else 2**63-1)).fetchall()
    return {'first': rows[0][0] if rows else 0, 'last': rows[-1][0] if rows else 0,
            'count': len(rows), 'hash': digest([list(r) for r in rows])}


def state_projection(store, session_id):
    task = store.task_for_session(session_id)
    session = store._conn.execute('SELECT memory_revision,context_generation FROM sessions WHERE session_id=?',
                                  (session_id,)).fetchone()
    if session is None:
        raise ValueError('会话不存在；不能猜造交接包')
    condition = 'task_id=?' if task else 'run_id IN (SELECT run_id FROM runs WHERE session_id=?)'
    identity = task['task_id'] if task else session_id
    operations = [store.operation_get(r[0]) for r in store._conn.execute(
        "SELECT operation_id FROM tool_operations WHERE " + condition
        + " AND state IN ('prepared','executing','unknown') ORDER BY rowid", (identity,))]
    questions = [] if task is None else [dict(r) for r in store._conn.execute(
        "SELECT * FROM task_questions WHERE task_id=? AND state<>'answered' ORDER BY rowid", (task['task_id'],))]
    source = None if task is None else task['source_ref']
    if task:
        for ref in [source, *task['clarifications']]:
            row = store._conn.execute('SELECT content,status FROM messages WHERE message_id=? AND session_id=?',
                                      (ref['message_id'], ref['session_id'])).fetchone()
            active = store._conn.execute("SELECT 1 FROM history_sources WHERE source_id=? AND status='active'",
                                         (ref['message_id'],)).fetchone()
            if not row or not active or row['content'] != ref['text']:
                raise ValueError('Task 原话来源缺失/撤回/变更；需要用户重新确认，禁止摘要复活')
    runtime = None if task is None else store._conn.execute('SELECT * FROM task_runtime WHERE task_id=?',
                                                           (task['task_id'],)).fetchone()
    return {'task_id': task['task_id'] if task else None,
            'task_revision': task['revision'] if task else None,
            'plan_revision': task['current_plan_revision'] if task else None,
            'task_status': task['status'] if task else None,
            'budget_used': {'calls': runtime['used_calls'], 'receipts': json.loads(runtime['receipts'])} if runtime else {},
            'budget_reserved': {'calls': runtime['reserved_calls'], 'active_run': runtime['active_run']} if runtime else {},
            'goal': {'text': task['goal'], 'source': source} if task else None,
            'constraints': [{'text': c, 'source': source} for c in task['constraints']] if task else [],
            'completed_steps': [s for s in task['steps'] if s['status'] == 'COMPLETED'] if task else [],
            'pending_steps': [s for s in task['steps'] if s['status'] != 'COMPLETED'] if task else [],
            'acceptance': task['acceptance'] if task else {},
            'clarifications': task['clarifications'] if task else [],
            'open_questions': questions,
            'operations_to_reconcile': operations,
            'memory_revision': session['memory_revision'],
            'context_generation': session['context_generation']}


def source_manifest(history):
    return [{'id': m.message_id, 'hash': m.source_hash} for m in history]


def important_facts(history):
    # 未结构化的用户输入可能在最后一句追加约束，绝不靠摘录器猜哪些可以删。
    # 无 Task 的旧会话仍无结构化 Task；这些是带来源的原话，不能猜成完成步骤。
    return [{'text': m.content, 'source': m.message_id, 'hash': m.source_hash, 'kind': 'user_statement'}
            for m in history if m.role == 'user']


def artifact_refs(value):
    return sorted(set(re.findall(r'artifact://[\w/-]+#[a-f0-9]+', encode(value))))


def verify_artifacts(store, refs, artifacts=None):
    # 只验证版本化元数据/撤回状态/文件存在；不把大 blob 读回上下文。
    for uri in refs:
        if artifacts is None:
            raise ValueError('compact 引用校验缺少当前项目 ArtifactStore；禁止猜测 data_dir')
        row = store._conn.execute('SELECT * FROM artifacts WHERE uri=?', (uri,)).fetchone()
        if row is None or store._conn.execute(
                'SELECT 1 FROM memory_artifact_blocks WHERE artifact_id=? AND version=?',
                (row['artifact_id'], row['version'])).fetchone():
            raise ValueError('compact 引用缺失或已撤回：' + uri)
        from core.artifacts import ArtifactError
        try:
            meta = artifacts._read_meta(row['artifact_id'], row['version'])
            path = artifacts._blob_path(meta)
            artifacts._check_source_fresh(meta)
        except ArtifactError as exc:
            raise ValueError('compact 引用无权访问：' + str(exc)) from exc
        if not path.is_file() or path.stat().st_size != row['size']:
            raise ValueError('compact 引用文件缺失或大小已变：' + uri)
        identity = store._conn.execute('SELECT project_id FROM history_identity WHERE id=1').fetchone()
        if identity and row['owner'] != identity[0]:
            raise ValueError('compact 引用不属于当前项目')
        # 流式校验完整性，内存有界；只投放 URI，绝不回填 blob 内容。
        hasher = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(65536), b''):
                hasher.update(chunk)
        if hasher.hexdigest() != row['hash']:
            raise ValueError('compact 引用 hash 已变：' + uri)
