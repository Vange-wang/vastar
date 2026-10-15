"""Vastar runtime component."""
import time
from dataclasses import dataclass, replace
from uuid import uuid4

from core.orchestration.contracts import TaskEnvelope, nonempty

QUESTION_STATES = frozenset({'pending', 'answered', 'expired'})


@dataclass(frozen=True)
class Question:
    """一条聚合后的求助：绑定 task/plan_revision，asked_by 记录全部提问 step。"""
    question_id: str
    task_id: str
    plan_revision: int
    text: str
    asked_by: tuple[str, ...]
    state: str = 'pending'
    answer: str | None = None
    asked_at: float = 0.0
    owner: str | None = None
    round_id: str = 'initial'

    def __post_init__(self):
        nonempty(self.question_id, 'question_id')
        nonempty(self.task_id, 'question_task')
        nonempty(self.text, 'question_text')
        if type(self.plan_revision) is not int or self.plan_revision < 1:
            raise ValueError('lifecycle_question_revision_invalid')
        if self.state not in QUESTION_STATES:
            raise ValueError('lifecycle_question_state_invalid')
        if (self.state == 'answered') != (self.answer is not None):
            raise ValueError('lifecycle_question_answer_mismatch')


def _normalize(text) -> str:
    if type(text) is not str or not text.strip():
        raise ValueError('lifecycle_question_invalid')
    return ' '.join(text.split())


class QuestionBroker:
    """Worker 求助的聚合与绑定校验（Lead 侧；不持有执行权）。"""

    def __init__(self, *, clock=time.time):
        self._clock = clock
        self._questions: dict[str, Question] = {}
        self._by_key: dict[tuple, str] = {}
        self._owners = {}
        self._closed = set()

    def emit(self, envelope: TaskEnvelope, text: str, *, round_id='initial') -> tuple[Question, bool]:
        if type(envelope) is not TaskEnvelope:
            raise ValueError('lifecycle_question_envelope_invalid')
        normalized = _normalize(text)
        nonempty(round_id, 'question_round')
        owner = self._owners.get((envelope.task_id, envelope.step_id))
        if envelope.task_id in self._closed and owner is None:
            raise ValueError('lifecycle_question_cancelled')
        key = (envelope.task_id, envelope.plan_revision, normalized, owner, round_id)
        existing = self._by_key.get(key)
        if existing is not None:
            q = self._questions[existing]
            if envelope.step_id not in q.asked_by:
                q = replace(q, asked_by=q.asked_by + (envelope.step_id,))
                self._questions[existing] = q
            return q, False
        q = Question(uuid4().hex, envelope.task_id, envelope.plan_revision, normalized,
                     (envelope.step_id,), asked_at=self._clock(), owner=owner, round_id=round_id)
        self._questions[q.question_id] = q
        self._by_key[key] = q.question_id
        return q, True

    def pending(self) -> tuple[Question, ...]:
        return tuple(q for q in self._questions.values() if q.state == 'pending')

    def get(self, question_id: str) -> Question:
        q = self._questions.get(question_id)
        if q is None:
            raise ValueError('lifecycle_question_unknown')
        return q

    def answer(self, question_id: str, text: str, *, plan_revision: int, verify=None) -> Question:
        q = self.get(question_id)
        if q.state == 'answered':
            raise ValueError('lifecycle_question_duplicate_answer')
        if q.state == 'expired':
            raise ValueError('lifecycle_question_expired')
        if type(plan_revision) is not int or plan_revision != q.plan_revision:
            # 过期答案：标 expired、不采信不应用（迟到答案不误用）
            q = replace(q, state='expired')
            self._questions[q.question_id] = q
            return q
        if not callable(verify) or verify(q, text) is not True:
            raise ValueError('lifecycle_answer_revalidation_failed')   # 回答前重验依赖与授权
        if self.get(question_id) != q:
            raise ValueError('lifecycle_question_changed_during_verify')
        q = replace(q, state='answered', answer=_normalize(text))
        self._questions[q.question_id] = q
        return q

    def expire_stale(self, current_revision: int) -> int:
        if type(current_revision) is not int or current_revision < 1:
            raise ValueError('lifecycle_question_revision_invalid')
        stale = [q for q in self.pending() if q.plan_revision < current_revision]
        for q in stale:
            self._questions[q.question_id] = replace(q, state='expired')
        return len(stale)

    def expire_pending(self) -> int:
        pending = self.pending()
        for q in pending:
            self._questions[q.question_id] = replace(q, state='expired')
        return len(pending)

    def transfer_owner(self, task_id, step_id, owner):
        self._owners[(task_id, step_id)] = owner
        for q in tuple(self._questions.values()):
            if q.task_id != task_id or step_id not in q.asked_by:
                continue
            if q.owner == owner:
                continue
            target = next((item for item in self._questions.values()
                           if (item.task_id, item.plan_revision, item.text, item.owner, item.round_id)
                           == (q.task_id, q.plan_revision, q.text, owner, q.round_id)), None)
            if target is not None:
                # Preserve consumed identity across ownership changes; never reopen a round.
                terminal = next((item for item in (target, q) if item.state == 'answered'), None)
                terminal = terminal or next((item for item in (target, q) if item.state == 'expired'), target)
                self._questions[target.question_id] = replace(
                    target, asked_by=tuple(dict.fromkeys((*target.asked_by, step_id))),
                    state=terminal.state, answer=terminal.answer)
                remaining = tuple(s for s in q.asked_by if s != step_id)
                self._questions[q.question_id] = replace(
                    q, asked_by=remaining, state='expired' if q.state == 'pending' and not remaining else q.state)
                continue
            if q.asked_by == (step_id,):
                self._questions[q.question_id] = replace(q, owner=owner)
            else:
                self._questions[q.question_id] = replace(q, asked_by=tuple(s for s in q.asked_by if s != step_id))
                child = replace(q, question_id=uuid4().hex, owner=owner, asked_by=(step_id,))
                self._questions[child.question_id] = child
        self._by_key = {(q.task_id, q.plan_revision, q.text, q.owner, q.round_id): q.question_id
                        for q in self._questions.values()}

    def cancel_task(self, task_id):
        self._closed.add(task_id)
        pending = [q for q in self.pending() if q.task_id == task_id and q.owner is None]
        for q in pending:
            self._questions[q.question_id] = replace(q, state='expired')
        return len(pending)
