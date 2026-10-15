"""B48 多 Agent 共享记忆治理：指定快照读 / 私有短期笔记 / proposal 队列 / 冲突记录 / 撤回传播。

纪律（逐条对应任务单 2026-10-12-b48 §二）：
- Worker 读指定 memory snapshot（稳定前缀，按 revision 钉版），不看全局实时；按需最新查询
  与稳定前缀分开标版本（snapshot()/latest() 各自带版本号）；
- 共享写入唯一入口 = 本服务的 proposal 队列；来源必须含 agent/task/artifact 三元，
  且 proposal.agent 必须等于提交者 profile_id（不可冒名）；
- drain 是共享事实的唯一提交者（与 B47「Integrator 唯一合并者」同构：文件产物走 Integrator，
  共享事实走本服务，两条写路径互不旁路；如口径冲突以更严者为准——本服务三者必查后再 CAS）。
  提交前确定性三项必查：scope（在 profile.memory_scope 内）/ 敏感信息 / expected_revision；
- 同时写同一事实：后到者 CAS 失败即落持久冲突记录（memory_conflicts），绝不静默覆盖；
- 用户原话权威最高：agent 提案 authority=1，覆盖 authority=2 的用户事实被 lower_authority 拒绝；
- 防自我强化：citations 只能引用已提交且仍 active 的共享事实（mem-<id>@r<rev>）；
  引用 agent 产出（authority<2）的事实时提案只能以 hypothesis 入库——传闻不能升级为事实；
  pending 数量 / 提案 TTL / 引用条数均有上限；
- 撤回复用 B23 撤回链（tombstone + clean_snapshots 通知活跃 context/checkpoint）；
  任务续跑必须先 revalidate：已撤回事实不得复活（tombstone 阻断同 scope/key 再提交）。
"""
import json
import re
import time
import uuid
from dataclasses import dataclass

from core.orchestration.contracts import nonempty, strings
from core.orchestration.shared_memory_read import SharedMemoryRead
from memory import policy
from memory.audit import MemoryAudit
from memory.forget_cleanup import apply_entry
from memory.forget_journal import append_intent, blocked, token
from memory.forget_plan import plan as forget_impact
from memory.repositories.facts import encode

AGENT_AUTHORITY = 1  # 用户原话 authority=2；任何 agent（含 lead 身份）都不能覆盖用户原话。
_CITATION = re.compile(r'(mem-[0-9a-f]+)@r(\d+)')


@dataclass(frozen=True)
class SharedProposal:
    """Worker 写共享记忆的唯一载体；来源三元 = agent/task_id/artifact_refs。"""

    key: str
    text: str
    kind: str
    scope: str
    agent: str
    task_id: str
    artifact_refs: tuple[str, ...]
    expected_revision: int
    citations: tuple[str, ...] = ()
    validity: dict | None = None
    replace: bool = False
    proposal_id: str = ''

    def __post_init__(self):
        for name in ('key', 'text', 'scope', 'agent', 'task_id'):
            nonempty(getattr(self, name), name)
        strings(self.artifact_refs, 'proposal_artifacts')
        strings(self.citations, 'proposal_citations')
        if not self.artifact_refs:
            raise ValueError('proposal_source_triple_incomplete')
        if type(self.expected_revision) is not int or self.expected_revision < 0:
            raise ValueError('worker_expected_revision_invalid')
        if type(self.replace) is not bool or self.proposal_id is None:
            raise ValueError('worker_proposal_metadata_invalid')
        if self.validity is not None and type(self.validity) is not dict:
            raise ValueError('worker_proposal_validity_invalid')


class SharedMemoryService(SharedMemoryRead):
    """多 Agent 共享记忆的唯一提交路径；由宿主构造、宿主 drain（worker 不直接写库）。"""

    def __init__(self, store, user_id, project_id, *, now=None, clock=None,
                 max_pending_per_agent=8, proposal_ttl_seconds=3600.0,
                 max_citations=4, max_notes_per_agent=16, note_ttl_seconds=900.0):
        self.store, self.user_id, self.project_id = store, user_id, project_id
        self._now = now or store._now
        self._clock = clock or time.monotonic
        self._max_pending = max_pending_per_agent
        self._proposal_ttl = float(proposal_ttl_seconds)
        self._max_citations = max_citations
        self._max_notes = max_notes_per_agent
        self._note_ttl = float(note_ttl_seconds)
        self._notes = {}
        store.bind_history_scope(user_id, project_id)

    # ---- 提案入队（worker 唯一写入口） -------------------------------------------

    def submit(self, profile, proposal):
        """校验来源与准入后入队；拒绝也持久化留痕。proposal_id 幂等。"""
        if type(proposal) is not SharedProposal:
            raise ValueError('shared_proposal_invalid')
        pid = proposal.proposal_id or 'smp-' + uuid.uuid4().hex
        record = self._record(profile, proposal)
        with self.store._lock:
            prior = self.store._conn.execute('SELECT record_json,receipt_json FROM agent_proposals WHERE id=?',
                                             (pid,)).fetchone()
            if prior:
                if prior['record_json'] != encode(record):
                    raise ValueError('proposal_id_parameter_conflict')
                return json.loads(prior['receipt_json'])
            try:
                self._check_admission(profile, proposal)
            except ValueError as exc:
                receipt = {'status': 'rejected', 'reason': str(exc), 'proposal_id': pid}
                expires = None
            else:
                receipt = {'status': 'pending', 'proposal_id': pid,
                           'note': '提案已入队；提交以 drain 的确定性三项必查为准。'}
                expires = self._clock() + self._proposal_ttl
            with self.store.transaction():
                self.store._conn.execute('INSERT INTO agent_proposals VALUES(?,?,?,?,?,?,?,?,?,?,NULL)',
                                         (pid, proposal.agent, proposal.task_id, proposal.scope, proposal.key,
                                          receipt['status'], encode(record), encode(receipt),
                                          self._now(), expires))
            return receipt

    def _check_admission(self, profile, proposal):
        if profile.role != 'worker':
            raise ValueError('reviewer_cannot_write_shared')
        if proposal.agent != profile.profile_id:
            raise ValueError('agent_identity_mismatch')
        if proposal.scope not in profile.permissions.memory_scope:
            raise ValueError('memory_scope_denied')
        policy.validate({'key': proposal.key, 'kind': proposal.kind, 'text': proposal.text,
                         'validity': proposal.validity or {}})
        if policy.sensitive(proposal.text) or policy.sensitive(proposal.key):
            raise ValueError('sensitive_credentials')
        if blocked(self.store, proposal.scope, proposal.key):
            raise ValueError('withdrawn_cannot_revive')
        if len(proposal.citations) > self._max_citations:
            raise ValueError('too_many_citations')
        pending = self.store._conn.execute(
            "SELECT count(*) FROM agent_proposals WHERE agent=? AND status='pending'",
            (proposal.agent,)).fetchone()[0]
        if pending >= self._max_pending:
            raise ValueError('proposal_rate_limited')

    @staticmethod
    def _record(profile, proposal):
        return {'key': proposal.key, 'text': proposal.text, 'kind': proposal.kind, 'scope': proposal.scope,
                'agent': proposal.agent, 'task_id': proposal.task_id,
                'artifact_refs': list(proposal.artifact_refs), 'expected_revision': proposal.expected_revision,
                'citations': list(proposal.citations), 'validity': proposal.validity, 'replace': proposal.replace,
                'memory_scope': sorted(profile.permissions.memory_scope)}

    # ---- 提交（宿主 drain：唯一提交者） --------------------------------------------

    def drain(self, *, limit=None):
        """FIFO 处理 pending 提案；每条独立事务，committed/rejected/conflict/expired 均留回执。"""
        receipts = []
        while limit is None or len(receipts) < limit:
            with self.store._lock:
                row = self.store._conn.execute(
                    "SELECT id,record_json,expires_at FROM agent_proposals WHERE status='pending'"
                    ' ORDER BY rowid LIMIT 1').fetchone()
            if row is None:
                return receipts
            receipts.append(self._decide(row))
        return receipts

    def _decide(self, row):
        pid, proposal = row['id'], json.loads(row['record_json'])
        with self.store.transaction():
            fresh = self.store._conn.execute(
                'SELECT status,receipt_json FROM agent_proposals WHERE id=?', (pid,)).fetchone()
            if fresh['status'] != 'pending':
                return json.loads(fresh['receipt_json'])
            receipt = self._judge(pid, proposal, row['expires_at'])
            receipt['proposal_id'] = pid
            self.store._conn.execute('UPDATE agent_proposals SET status=?,receipt_json=?,decided_at=? WHERE id=?',
                                     (receipt['status'], encode(receipt), self._now(), pid))
        return receipt

    def _judge(self, pid, proposal, expires_at):
        if expires_at is not None and self._clock() > expires_at:
            return {'status': 'expired', 'reason': 'proposal_ttl_expired'}
        try:
            if blocked(self.store, proposal['scope'], proposal['key']):
                raise ValueError('withdrawn_cannot_revive')
            cited = self._ground_citations(proposal)
            if cited and any(item['authority'] < 2 for item in cited) and proposal['kind'] != 'hypothesis':
                raise ValueError('hearsay_cannot_become_fact')
            if policy.sensitive(proposal['text']) or policy.sensitive(proposal['key']):
                raise ValueError('sensitive_credentials')
            validity = policy.validate({'key': proposal['key'], 'kind': proposal['kind'],
                                        'text': proposal['text'], 'validity': proposal['validity'] or {}})
            old = self.store.memory_item(proposal['scope'], proposal['key'])
            normalized = {'key': proposal['key'], 'kind': proposal['kind'], 'text': proposal['text'],
                          'validity': validity, 'expected_revision': proposal['expected_revision'],
                          'replace': proposal['replace']}
            action = policy.decide_update(old, normalized, AGENT_AUTHORITY, self._now())
            if proposal['kind'] != 'hypothesis' and not any(
                    i['authority'] >= 2 and i['text'] == proposal['text'] for i in cited):
                raise ValueError('agent_fact_requires_verified_source')
        except ValueError as exc:
            return self._reject_or_conflict(pid, proposal, str(exc))
        if action == 'duplicate':
            return {'status': 'duplicate', 'item': old, 'memory_revision': self.store.memory_revision()}
        item = self._commit(pid, proposal, old, validity)
        return {'status': 'committed', 'item': item, 'memory_revision': item['revision']}

    def _reject_or_conflict(self, pid, proposal, reason):
        old = self.store.memory_item(proposal['scope'], proposal['key'])
        if reason != 'expected_revision_conflict' or old is None:
            return {'status': 'rejected', 'reason': reason}
        row = self.store._conn.execute('SELECT proposal_id FROM memory_revisions WHERE revision=?',
                                       (old['revision'],)).fetchone()
        cid = 'smc-' + uuid.uuid4().hex
        record = {'scope': proposal['scope'], 'key': proposal['key'],
                  'winner': {'revision': old['revision'], 'text': old['text'], 'origin': old['origin'],
                             'proposal_id': row['proposal_id'] if row else None},
                  'loser': {'proposal_id': pid, 'agent': proposal['agent'], 'task_id': proposal['task_id'],
                            'text': proposal['text'], 'expected_revision': proposal['expected_revision']},
                  'rule': 'concurrent_write_no_silent_overwrite'}
        self.store._conn.execute('INSERT INTO memory_conflicts VALUES(?,?,?,?,?)',
                                 (cid, proposal['scope'], proposal['key'], encode(record), self._now()))
        from memory.forget_agents import invalidate_conflict
        invalidate_conflict(self.store, old)
        return {'status': 'conflict', 'reason': reason, 'conflict_id': cid,
                'note': '同时写同一事实：后到者不覆盖，冲突记录持久化并传播给所有消费者。'}

    def _ground_citations(self, proposal):
        cited = []
        for ref in proposal['citations']:
            match = _CITATION.fullmatch(ref)
            item = self._item_by_id(match.group(1)) if match else None
            if item is None or ref not in self.revalidate([ref])['current']:
                raise ValueError('citation_not_grounded')
            if item['scope'] not in proposal['memory_scope']:
                raise ValueError('citation_scope_denied')
            cited.append(item)
        return cited

    def _commit(self, pid, proposal, old, validity):
        item = {'id': old['id'] if old else 'mem-' + uuid.uuid4().hex,
                'scope': proposal['scope'], 'kind': proposal['kind'], 'key': proposal['key'],
                'text': proposal['text'],
                'source_refs': [f"agent:{proposal['agent']}/{proposal['task_id']}", *proposal['artifact_refs']],
                'validity': validity, 'last_verified': self._now(), 'status': 'active',
                'expected_revision': proposal['expected_revision'],
                'supersedes': old['revision'] if old else None, 'sensitivity': 'normal',
                'authority': AGENT_AUTHORITY, 'origin': 'agent',
                'citations': list(proposal['citations'])}
        MemoryAudit(self.store._conn).record_commit(item, pid, proposal['agent'])
        self.store.memory_publish(item)
        return item

    # ---- 读取：稳定前缀与按需最新查询分开标版本 -------------------------------------

    # ---- 撤回（宿主操作；复用 B23 撤回链传播到 context/checkpoint） -------------------

    def withdraw(self, scope, key, *, expected_revision, by='host', artifacts=None):
        """撤回共享事实：tombstone 防复活 + clean_snapshots 通知活跃 context/checkpoint。"""
        item = self.store.memory_item(scope, key)
        if item is None:
            raise ValueError('memory_not_found')
        if type(expected_revision) is not int or expected_revision != item['revision']:
            raise ValueError('expected_revision_conflict')
        with self.store._lock:
            impact = forget_impact(self.store, item, artifacts)
            entry = {'action_id': 'smw-' + uuid.uuid4().hex,
                     'fingerprint': token(scope, key, expected_revision),
                     'mode': 'withdraw', 'actor': by, 'origin': item['origin'],
                     'time': self._now(), 'impact': impact}
            append_intent(self.store, entry)  # 先落意图，崩溃后由 Store 启动重放。
            with self.store.transaction():
                apply_entry(self.store, entry)
        return {'status': 'committed', 'mode': 'withdraw', 'action_id': entry['action_id'],
                'affected_refs': {'items': impact['items'], 'sessions': impact['sessions'],
                                  'contexts': impact['contexts'], 'checkpoints': impact['checkpoints']}}

    # ---- 私有短期笔记（不进共享库，worker 私有） --------------------------------------

    def note(self, profile, text):
        if not isinstance(text, str) or not text.strip():
            raise ValueError('note_text_required')
        notes = self._notes.setdefault(profile.profile_id, [])
        notes.append({'text': text, 'at': self._clock()})
        while len(notes) > self._max_notes:
            notes.pop(0)
        return f'note:{profile.profile_id}:{len(notes)}'

    def notes(self, profile):
        now = self._clock()
        kept = [n for n in self._notes.get(profile.profile_id, []) if now - n['at'] <= self._note_ttl]
        self._notes[profile.profile_id] = kept
        return tuple(n['text'] for n in kept)

    # ---- 查询（证据/消费者可见性） ----------------------------------------------------

    def proposals(self, *, status=None):
        sql = 'SELECT id,agent,task_id,scope,key,status,receipt_json FROM agent_proposals'
        with self.store._lock:
            rows = self.store._conn.execute(sql + (' WHERE status=?' if status else '') + ' ORDER BY rowid',
                                            (status,) if status else ()).fetchall()
        return [{'proposal_id': r['id'], 'agent': r['agent'], 'task_id': r['task_id'], 'scope': r['scope'],
                 'key': r['key'], 'status': r['status'], 'receipt': json.loads(r['receipt_json'])} for r in rows]

    def conflicts(self, *, scope=None):
        sql = 'SELECT id,scope,key,record_json,created_at FROM memory_conflicts'
        with self.store._lock:
            rows = self.store._conn.execute(sql + (' WHERE scope=?' if scope else '') + ' ORDER BY rowid',
                                            (scope,) if scope else ()).fetchall()
        return [{'conflict_id': r['id'], 'scope': r['scope'], 'key': r['key'],
                 **json.loads(r['record_json']), 'created_at': r['created_at']} for r in rows]
