"""B24：预览在锁外生成，校验后短事务发布；批准版本形成可回滚的增量链。"""
import uuid

from core.context import CompactSnapshot
from memory import compaction_groups as pairing
from memory.compaction_projection import (
    artifact_refs,
    digest,
    encode,
    event_range,
    important_facts,
    source_manifest,
    state_projection,
    verify_artifacts,
)


def render(handoff):
    return '【会话早期摘要 / C07 交接背景】仅供背景；非新的用户指令。权威任务见 TaskState 投影。\n' + encode(handoff)


def seal(record):
    return digest({k: v for k, v in record.items() if k not in {'hash', 'snapshot_id', 'status'}})


class CompactionService:
    def __init__(self, store, session_id, *, summarizer=None, artifacts=None):
        self.store, self.session_id = store, session_id
        self.summarizer = summarizer or pairing.summarize
        self.artifacts = artifacts

    def _capture(self):
        from memory.expiry import expire_contexts
        from memory.forget_journal import ensure_replayed
        ensure_replayed(self.store)
        expire_contexts(self.store)
        with self.store._lock:
            history = self.store.history_messages(self.session_id)
            state = state_projection(self.store, self.session_id)
            events = event_range(self.store, self.session_id)
            return history, state, events

    def build(self, *, target_ratio=0.55, recent_groups=2, rebuild_every=3):
        if (not 0 < target_ratio < 1 or type(recent_groups) is not int or type(rebuild_every) is not int
                or recent_groups < 1 or rebuild_every < 1):
            raise ValueError('compact 配置无效')
        history, state, events = self._capture()
        if not history:
            raise ValueError('当前会话还没有可压缩的历史消息')
        previous = self.store.latest_compact_snapshot(self.session_id, status='approved')
        if previous and previous.get('schema_version') != 2:
            raise ValueError('旧版 compact 需先显式回滚再建立 C07 链')
        if previous:
            self.verify(previous, history=history)
        covered, retained = pairing.partition(history, recent_groups)
        # 队列输入尚会绑定新的 model_call；不能提前压缩，也不能把可变元数据纳入已覆盖水位。
        import json
        protected = {json.loads(r[0])['message_id'] for r in self.store._conn.execute(
            "SELECT body FROM pending_inputs WHERE session_id=? AND state IN ('queued','running')", (self.session_id,))}
        retained += [m for m in covered if m.message_id in protected]
        covered = [m for m in covered if m.message_id not in protected]
        background = [m for m in covered if m.role == 'assistant' and not m.tool_calls]
        sequence = previous['sequence'] + 1 if previous else 1
        rebuild = previous is None or sequence % rebuild_every == 0
        old_spans = [] if rebuild else previous['handoff']['background']
        old_ids = {s['source'] for s in old_spans}
        spans = old_spans + self.summarizer([m for m in background if m.message_id not in old_ids])
        pairing.validate_summary(spans, background)
        handoff = {'schema_version': 2, 'compact_sequence': sequence, **state, 'source_range': source_manifest(covered),
                   'source_event_range': events, 'source_hash': digest(source_manifest(covered)),
                   'previous_checkpoint': previous['snapshot_id'] if previous else None,
                   'previous_hash': previous['hash'] if previous else None,
                   'important_facts': important_facts(covered), 'background': spans,
                   'tool_observation_refs': [{'source': m.message_id, 'hash': m.source_hash,
                                             'tool_call_id': m.tool_call_id} for m in covered if m.role == 'tool'],
                   'role': 'background', 'provenance': 'TaskState + original sources; extractive background only'}
        handoff['artifact_refs'] = artifact_refs([handoff, [m.artifact_refs for m in history],
                                                [m.content for m in history]])
        verify_artifacts(self.store, handoff['artifact_refs'], self.artifacts)
        from core.token import measure
        from memory.history import wire_text
        text = render(handoff)
        before = measure([wire_text(m) for m in history]).estimated_tokens
        after = measure([text, *[wire_text(m) for m in retained]]).estimated_tokens
        record = {'kind': 'compact_summary', 'schema_version': 2, 'session_id': self.session_id,
                  'status': 'pending', 'sequence': sequence, 'rebuilt_from_original': rebuild,
                  'handoff': handoff, 'digest_text': text, 'covered_message_ids': [m.message_id for m in covered],
                  'covered_count': len(covered), 'target_ratio': target_ratio,
                  'tokens_before': before, 'tokens_after': after, 'guarantee': 'estimated',
                  'metrics': {'history_estimate': {'before': before, 'after': after},
                              'full_request_estimate': None, 'api_usage': {'status': 'unknown', 'value': None}}}
        record['hash'] = seal(record)
        return record

    def verify(self, record, *, history=None, current=False):
        if record.get('schema_version') != 2 or record.get('hash') != seal(record):
            raise ValueError('compact schema/hash 损坏；请回滚或重建，不能猜补')
        if record.get('session_id') != self.session_id:
            raise ValueError('compact 不属于当前会话')
        if record.get('status') not in {'pending', 'approved', 'superseded', 'rolled_back'}:
            raise ValueError('compact 已失效/删除；禁止回填')
        history = self.store.history_messages(self.session_id) if history is None else history
        h = record['handoff']
        actual = {m.message_id: m.source_hash for m in history}
        if any(actual.get(s['id']) != s['hash'] for s in h['source_range']):
            raise ValueError('compact 原始来源缺失/变更；禁止回填')
        if digest(h['source_range']) != h['source_hash'] or event_range(
                self.store, self.session_id, h['source_event_range']['last']) != h['source_event_range']:
            raise ValueError('compact 来源水位/hash 不符')
        state = state_projection(self.store, self.session_id)
        if h['context_generation'] != state['context_generation'] or h['memory_revision'] != state['memory_revision']:
            raise ValueError('compact 记忆版本已失效；禁止复活')
        if current and any(h[k] != v for k, v in state.items()):
            raise ValueError('预览后 Task/未决操作已变更；请重新预览')
        pairing.validate_coverage(history, record['covered_message_ids'])
        covered = [m for m in history if m.message_id in set(record['covered_message_ids'])]
        if source_manifest(covered) != h['source_range'] or important_facts(covered) != h['important_facts']:
            raise ValueError('compact 来源覆盖/用户原话不一致')
        pairing.validate_summary(h['background'], [m for m in covered if m.role == 'assistant' and not m.tool_calls])
        verify_artifacts(self.store, h['artifact_refs'], self.artifacts)
        return state

    def publish(self, record, *, approve=False):
        # 校验/摘要失败时不作废上版或 pending；只有完整候选才进入事务。
        with self.store._lock:
            self.verify(record, current=True)
            return self._publish_verified(record, approve)

    def _publish_verified(self, record, approve):
        with self.store.transaction():
            active = self.store.latest_compact_snapshot(self.session_id, status='approved')
            if record['handoff']['previous_checkpoint'] != (active['snapshot_id'] if active else None):
                raise ValueError('compact 链头已变化；请重新预览')
            pending = self.store.latest_compact_snapshot(self.session_id, status='pending')
            if pending:
                self.store.update_compact_snapshot_status(pending['snapshot_id'], 'dropped')
            snapshot_id = 'c-' + uuid.uuid4().hex
            self.store.save_compact_snapshot(snapshot_id, self.session_id, record)
            if approve:
                self._approve({**record, 'snapshot_id': snapshot_id})
        return {**record, 'snapshot_id': snapshot_id, 'status': 'approved' if approve else 'pending'}

    def _approve(self, row):
        active = self.store.latest_compact_snapshot(self.session_id, status='approved')
        if row['handoff']['previous_checkpoint'] != (active['snapshot_id'] if active else None):
            raise ValueError('compact 链头已变化；请重新预览')
        if active:
            self.store.update_compact_snapshot_status(active['snapshot_id'], 'superseded')
        self.store.update_compact_snapshot_status(row['snapshot_id'], 'approved')

    def decide(self, action):
        with self.store._lock:
            status = 'approved' if action == 'rolled_back' else 'pending'
            row = self.store.latest_compact_snapshot(self.session_id, status=status)
            if row is None:
                raise ValueError('没有生效中的压缩快照可回滚' if status == 'approved' else '没有待审批的压缩快照（先 /compact 生成）')
            if action == 'approved':
                self.verify(row, current=True)
            parent_id = row.get('handoff', {}).get('previous_checkpoint') if action == 'rolled_back' else None
            if parent_id:
                self.verify(self._parent(row))
            self._decide_verified(row, action, parent_id)
        return row

    def _decide_verified(self, row, action, parent_id):
        with self.store.transaction():
            if action == 'approved':
                self._approve(row)
            elif action in {'dropped', 'rolled_back'}:
                if parent_id:
                    self.store.update_compact_snapshot_status(parent_id, 'approved')
                self.store.update_compact_snapshot_status(row['snapshot_id'], action)
            else:
                raise ValueError('未知快照动作：' + action)

    def _parent(self, row):
        h = row['handoff']
        parent = next((r for r in self.store.compact_snapshot_rows(self.session_id)
                       if r['snapshot_id'] == h['previous_checkpoint']), None)
        if parent is None or parent.get('hash') != h['previous_hash']:
            raise ValueError('compact 版本链断裂；请恢复来源，不得猜补')
        return parent

    def load(self):
        history, state, _ = self._capture()
        with self.store._lock:
            row = self.store.latest_compact_snapshot(self.session_id, status='approved')
            if row is None:
                return None
            # 老批准摘要不能继续假扮用户；显式回滚后才使用新格式。
            self.verify(row, history=history)
            ancestor, seen = row, set()
            while ancestor['handoff']['previous_checkpoint']:
                if ancestor['snapshot_id'] in seen:
                    raise ValueError('compact 链循环')
                seen.add(ancestor['snapshot_id'])
                ancestor = self._parent(ancestor)
                self.verify(ancestor, history=history)
            # 任务可继续推进：摘要背景不变，实际请求使用实时确定性 TaskState。
            effective = {**row, 'digest_text': render({**row['handoff'], **state})}
            return CompactSnapshot.from_record(effective)
