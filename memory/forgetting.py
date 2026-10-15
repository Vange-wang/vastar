"""T23 服务：预览绑定 CAS/影响集；撤回、回滚、硬删除是三条不同状态迁移。"""
import json
import uuid

from memory import policy
from memory.audit import MemoryAudit
from memory.fact_sources import verify_sources
from memory.forget_cleanup import apply_entry, purge_artifacts
from memory.forget_journal import append_intent, read_journal, token
from memory.forget_plan import plan

MODES = {'withdraw', 'rollback', 'hard_delete'}
BACKUP_NOTICE = ('受管库与受管产物的召回封锁不等于物理擦除；数据库空闲页/WAL、SSD历史块、外部备份、'
                 '已发送供应商内容不保证物理擦除。恢复必须保留最新撤回日志；不得用旧日志覆盖。')


def compatible_impact(old, new):
    # 预览声明清理整批会话派生输出；确认交互新增的同域派生副本也纳入。
    # 新事实版本、跨会话/跨scope扩张、额外不能清理的表面均需重做预览。
    return (old['item_versions'] == new['item_versions'] and old['sessions'] == new['sessions']
            and old['incomplete'] == new['incomplete'])


class ForgettingService:
    def __init__(self, memory, artifacts=None, *, fault=lambda point: None):
        self.memory, self.store, self.artifacts = memory, memory.store, artifacts
        self.fault = fault
        self._previews = {}

    def _target(self, session_id, request):
        if set(request) - {'key', 'scope', 'mode', 'expected_revision', 'target_revision'}:
            raise ValueError('unsupported_forget_fields')
        if request.get('mode') not in MODES:
            raise ValueError('invalid_delete_semantics')
        scope = policy.scope_for(self.memory, session_id, request.get('scope'))
        item = self.store.memory_item(scope, request['key'])
        if item is None:
            raise ValueError('memory_not_found')
        if type(request.get('expected_revision')) is not int or request['expected_revision'] != item['revision']:
            raise ValueError('expected_revision_conflict')
        if request['mode'] != 'rollback' and 'target_revision' in request:
            raise ValueError('target_revision_only_for_rollback')
        if request['mode'] == 'rollback':
            self._rollback_target(session_id, request, item)
        return item

    def preview(self, session_id, request):
        with self.store._lock:
            item = self._target(session_id, request)
            impact = plan(self.store, item, self.artifacts)
            if request['mode'] == 'rollback':
                impact = {**impact, 'items': [item['id']], 'sources': [], 'tokens': [], 'artifacts': []}
            preview = {'status': 'preview', 'mode': request['mode'], 'item_id': item['id'],
                    'expected_revision': item['revision'], 'impact': impact,
                    'preview_id': token(session_id, request, {k: v for k, v in impact.items() if k != 'runs'}),
                    'authorization': '仅当前用户明确确认；项目内 user/project/current-task/agent 域；不跨库/他人域',
                    'backup_notice': BACKUP_NOTICE}
            self._previews[preview['preview_id']] = (session_id, dict(request), preview)
            return preview

    def execute(self, session_id, request, preview_id, *, user_confirmed=False, action_id=None, cancel_event=None):
        if not user_confirmed:
            raise ValueError('forget_requires_explicit_user_confirmation')
        if cancel_event is not None and cancel_event.is_set():
            raise ValueError('cancelled_before_commit')
        aid = action_id or 'forget-' + uuid.uuid4().hex
        fingerprint = token(session_id, request, preview_id)
        with self.store._lock:
            row = self.store._conn.execute('SELECT record_json,receipt_json FROM memory_proposals WHERE id=?',
                                          ('rollback:' + aid,)).fetchone()
            if row:
                if json.loads(row[0]).get('fingerprint') != fingerprint:
                    raise ValueError('action_id_parameter_conflict')
                return json.loads(row[1])
            journal = read_journal(self.store)
            prior = next((e for e in journal['entries'] if e['action_id'] == aid), None)
            if prior:
                if prior['fingerprint'] != fingerprint:
                    raise ValueError('action_id_parameter_conflict')
                with self.store.transaction():
                    apply_entry(self.store, prior)
                return self._receipt(prior)
            saved = self._previews.get(preview_id)
            preview = self.preview(session_id, request)
            if saved is None or saved[:2] != (session_id, request) or not compatible_impact(saved[2]['impact'], preview['impact']):
                raise ValueError('impact_changed_preview_again')
            item = self._target(session_id, request)
            if request['mode'] == 'rollback':
                return self._rollback(session_id, request, item, preview, aid, fingerprint)
            entry = {'action_id': aid, 'fingerprint': fingerprint, 'mode': request['mode'],
                     'actor': session_id, 'origin': item['origin'], 'time': self.store._now(),
                     'impact': preview['impact']}
            append_intent(self.store, entry)  # 正文不落日志；已确认意图在事务失败时仍须恢复执行。
            self.store._privacy_pending = True
            self.fault('intent_durable')
            with self.store.transaction():
                apply_entry(self.store, entry)
                self.fault('before_commit')
            return self._receipt(entry)

    def _receipt(self, entry):
        from memory.forget_buffers import clear_caches
        self.store._privacy_pending = False
        clear_caches(self.store, entry)
        pending = sorted(set(entry['impact']['incomplete'] + purge_artifacts(self.store, self.artifacts)))
        return {'status': 'cleanup_pending' if pending else 'committed', 'action_id': entry['action_id'],
                'mode': entry['mode'], 'affected_refs': entry['impact'], 'incomplete_cleanup': pending,
                'backup_notice': BACKUP_NOTICE, 'physical_erasure': 'not_guaranteed'}

    def _rollback_target(self, session_id, request, item):
        if item['status'] != 'active':
            raise ValueError('withdrawn_cannot_revive')
        row = self.store._conn.execute('SELECT item_json FROM memory_revisions WHERE revision=? AND key=?',
                                      (request.get('target_revision'), item['id'])).fetchone()
        if not row or not row[0]:
            raise ValueError('rollback_revision_unavailable')
        target = json.loads(row[0])
        if target['revision'] >= item['revision'] or not policy.active(target, self.store._now()):
            raise ValueError('rollback_requires_earlier_valid_revision')
        verify_sources(self.memory, session_id, target)
        return target

    def _rollback(self, session_id, request, item, preview, aid, fingerprint):
        from memory.forget_cleanup import clean_snapshots
        with self.store.transaction():
            target = self._rollback_target(session_id, request, item)
            target.update(supersedes=item['revision'], expected_revision=item['revision'], last_verified=self.store._now())
            MemoryAudit(self.store._conn).record_commit(target, aid, session_id)
            self.store._conn.execute("UPDATE memory_revisions SET operation='rollback' WHERE revision=?", (target['revision'],))
            self.store.memory_publish(target)
            clean_snapshots(self.store._conn, {'impact': preview['impact'], 'action_id': aid}, False)
            receipt = {'status': 'committed', 'mode': 'rollback', 'revision': target['revision'],
                       'affected_refs': preview['impact'], 'incomplete_cleanup': [],
                       'note': '新修订引用旧有效内容；不撤销撤回/硬删除，不降低修订水位。'}
            self.store.memory_proposal('rollback:' + aid, session_id, {'fingerprint': fingerprint}, receipt)
            self.fault('rollback_before_commit')
        return receipt

    def cleanup(self):
        from memory.forget_journal import ensure_replayed
        with self.store._lock:
            ensure_replayed(self.store)
            pending = purge_artifacts(self.store, self.artifacts)
            entries = [json.loads(r[0]) for r in self.store._conn.execute('SELECT record_json FROM memory_actions')]
            pending += [p for e in entries for p in e['impact']['incomplete']]
            return {'status': 'cleanup_pending' if pending else 'committed',
                    'incomplete_cleanup': sorted(set(pending)), 'action_ids': [e['action_id'] for e in entries],
                    'note': '只重试已确认删除的受管blob；外部备份及需要人工处理的Task定义不虚报清完。'}

    def revalidation_queue(self, session_id):
        scopes = {policy.scope_for(self.memory, session_id, None), 'user:' + self.memory.user_id}
        return [{'id': i['id'], 'kind': i['kind'], 'revision': i['revision'], 'status': 'needs_revalidation'}
                for i in self.store.memory_current(scopes)
                if not policy.active(i, self.store._now())]
