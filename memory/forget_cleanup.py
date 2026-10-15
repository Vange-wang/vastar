"""事务内清除召回面；物理 blob 删除在事务后，可重试且读取端先封锁。"""
import json

from memory.repositories.facts import encode

REDACTED = '[已隐私删除]'


def apply_entry(store, entry, *, repeat=False):
    db, impact = store._conn, entry['impact']
    prior = db.execute('SELECT 1 FROM memory_actions WHERE action_id=?', (entry['action_id'],)).fetchone()
    if prior and not repeat:
        return
    hard = entry['mode'] == 'hard_delete'
    for key in impact['tokens']:
        db.execute('INSERT OR IGNORE INTO memory_tombstones VALUES(?,?)', (key, entry['action_id']))
    for iid in impact['items']:
        db.execute('DELETE FROM memory_index WHERE item_id=?', (iid,))
        if hard:
            db.execute('DELETE FROM memory_items WHERE id=?', (iid,))
            db.execute("UPDATE memory_revisions SET value=NULL,item_json=NULL,deleted=1 WHERE key=?", (iid,))
        else:
            db.execute("UPDATE memory_items SET status='withdrawn' WHERE id=?", (iid,))
    clean_sources(db, impact, hard)
    clean_snapshots(db, entry, bool(prior))
    if hard:
        from memory.forget_agents import clean_agent_rows
        clean_agent_rows(db, impact)
        clean_private_rows(db, impact)
        clean_audit_rows(db, impact.get('audit_rows', []))
    for meta in impact['artifacts']:
        insert = 'INSERT OR REPLACE' if hard else 'INSERT OR IGNORE'
        db.execute(insert + ' INTO memory_artifact_blocks VALUES(?,?,?)',
                   (meta['artifact_id'], meta['version'], entry['action_id']))
        if hard:
            db.execute("UPDATE artifacts SET source_ref='',source_hash=NULL WHERE artifact_id=? AND version=?",
                       (meta['artifact_id'], meta['version']))
    db.execute('INSERT OR IGNORE INTO memory_actions VALUES(?,?)', (entry['action_id'], encode(entry)))


def clean_sources(db, impact, hard):
    ids = set(impact['sources'])
    # 删除期间已经在 writer 缓冲中的旧 Run 后续事件不能重新注入摘要。
    for row in db.execute('SELECT * FROM history_sources'):
        if row['run_id'] in impact['runs']:
            ids.add(row['source_id'])
    for sid in ids:
        row = db.execute('SELECT id FROM history_sources WHERE source_id=?', (sid,)).fetchone()
        if row:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='history_fts'").fetchone():
                db.execute('DELETE FROM history_fts WHERE rowid=?', (row[0],))
            db.execute('DELETE FROM history_index_state WHERE id=?', (row[0],))
        db.execute("UPDATE history_sources SET status='withdrawn' WHERE source_id=?", (sid,))
        if hard:
            db.execute("UPDATE messages SET content=?,reasoning_content='',status='deleted' WHERE message_id=?", (REDACTED, sid))
            db.execute("UPDATE history_sources SET body='',record_json='{}',status='deleted' WHERE source_id=?", (sid,))
            # UPDATE 触发器会留下旧版本，所以必须最后清版本。
            db.execute('DELETE FROM history_versions WHERE source_id=?', (sid,))


def clean_snapshots(db, entry, prior):
    impact = entry['impact']
    for sid in impact['sessions']:
        row = db.execute('SELECT memory_snapshot_json FROM sessions WHERE session_id=?', (sid,)).fetchone()
        if row:
            items = [i for i in json.loads(row[0] or '[]') if i['id'] not in impact['items']]
            db.execute('UPDATE sessions SET memory_snapshot_json=?,context_rebuild=1,'
                       'context_generation=context_generation+? WHERE session_id=?', (encode(items), not prior, sid))
    contexts = set(impact['contexts']) | {r[0] for r in db.execute('SELECT snapshot_id,run_id FROM context_snapshots')
                                        if r[1] in impact['runs']}
    for cid in contexts:
        row = db.execute('SELECT record_json FROM context_snapshots WHERE snapshot_id=?', (cid,)).fetchone()
        if row:
            old = json.loads(row[0])
            record = {'kind': old.get('kind'), 'session_id': old.get('session_id'), 'status': 'needs_rebuild',
                      'reason': 'memory_revoked', 'action_id': entry['action_id']}
            db.execute('UPDATE context_snapshots SET record_json=? WHERE snapshot_id=?', (encode(record), cid))
    for cid in impact['checkpoints']:
        db.execute("UPDATE checkpoints SET state='needs_rebuild',body=?,hash='' WHERE checkpoint_id=?",
                   (encode({'reason': 'memory_revoked', 'action_id': entry['action_id']}), cid))


def clean_private_rows(db, impact):
    for row in db.execute('SELECT * FROM memory_proposals').fetchall():
        if (row['session_id'] in impact['sessions'] or
                any(i in row['receipt_json'] for i in impact['items'])):
            db.execute("UPDATE memory_proposals SET record_json='{}',receipt_json=?,status='redacted' WHERE id=?",
                       (encode({'status': 'redacted', 'reason': 'privacy_deleted'}), row['id']))
    for rid in impact['runs']:
        # 保留运行/操作身份、终态、usage 与预算；擦除会复制原文的内容字段。
        db.execute("UPDATE runs SET result_json=NULL WHERE run_id=?", (rid,))
        db.execute("UPDATE attempts SET response_frames=NULL,error_json=NULL WHERE run_id=?", (rid,))
        from memory.forget_buffers import safe_payload
        for row in db.execute('SELECT event_id,type,channel,payload FROM events WHERE run_id=?', (rid,)).fetchall():
            db.execute('UPDATE events SET payload=? WHERE event_id=?',
                       (encode(safe_payload(row['type'], row['channel'], json.loads(row['payload']))), row['event_id']))
        db.execute("DELETE FROM history_model_calls WHERE run_id=?", (rid,))
        db.execute("UPDATE pending_inputs SET state='cancelled',body='{}' WHERE run_id=?", (rid,))


def purge_artifacts(store, artifacts):
    pending = []
    for row in store._conn.execute('SELECT b.artifact_id,b.version,a.record_json FROM memory_artifact_blocks b '
                                   'JOIN memory_actions a ON a.action_id=b.action_id'):
        if json.loads(row[2])['mode'] != 'hard_delete':
            continue  # 撤回封锁引用但留原始审计内容；只有硬删除才 unlink。
        if artifacts is None:
            pending.append(f'artifact_store_unavailable:{row[0]}:v{row[1]}')
            continue
        try:
            meta = {'artifact_id': row[0], 'version': row[1]}
            path = artifacts._blob_path(meta)
            if path.resolve().parent != artifacts._require_dir().resolve():
                raise ValueError('artifact_path_outside_store')
            path.unlink(missing_ok=True)
        except (OSError, ValueError) as exc:
            pending.append(f'artifact_unlink_pending:{row[0]}:v{row[1]}:{type(exc).__name__}')
    return pending


def clean_audit_rows(db, rows):
    for ref in rows:
        table, rid = ref['table'], ref['rowid']
        if table == 'tool_operations':
            row = db.execute('SELECT tool,state FROM tool_operations WHERE rowid=?', (rid,)).fetchone()
            if row:
                receipt = {'name': row['tool'], 'status': 'executed' if row['state'] == 'committed' else 'unknown',
                           'reason': '历史回执正文已隐私删除，不允许据此重新执行', 'origin': 'privacy_redacted'}
                db.execute("UPDATE tool_operations SET facts='{}',receipt=?,reconcile_result=NULL,resource_version='{}' WHERE rowid=?",
                           (encode(receipt), rid))
        elif table == 'events':
            from memory.forget_buffers import safe_payload
            row = db.execute('SELECT type,channel,payload FROM events WHERE rowid=?', (rid,)).fetchone()
            if row:
                value = safe_payload(row['type'], 'privacy', json.loads(row['payload']))
                db.execute('UPDATE events SET payload=? WHERE rowid=?', (encode(value), rid))
        else:
            # 固定映射，日志数据不能变成任意 SQL 表名/列名。
            statements = {'agent_proposals': "UPDATE agent_proposals SET key='',record_json='{}',receipt_json='{}',status='redacted' WHERE rowid=?",
                          'memory_conflicts': 'DELETE FROM memory_conflicts WHERE rowid=?',
                          'attempts': "UPDATE attempts SET request_params='{}',error_json=NULL,response_frames=NULL WHERE rowid=?",
                          'history_versions': "UPDATE history_versions SET body='',record_json='{}',status='deleted' WHERE rowid=?",
                          'memory_revisions': 'UPDATE memory_revisions SET value=NULL,item_json=NULL,deleted=1 WHERE rowid=?',
                          'history_model_calls': "UPDATE history_model_calls SET record_json='{}' WHERE rowid=?"}
            db.execute(statements[table], (rid,))
