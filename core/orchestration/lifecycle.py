"""Vastar runtime component."""
import time

from core.orchestration.children import ChildSupervisor
from core.orchestration.questions import QuestionBroker

NEEDS_INPUT_PREFIX = 'needs_input:'
_RETRYABLE_CATEGORIES = frozenset({'transient', 'timeout'})


class LifecycleController:
    """父子生命周期编排：取消树 / 独立所有权 / watchdog / 可重试判定 / 问题聚合接入。"""

    def __init__(self, supervisor: ChildSupervisor, broker: QuestionBroker | None = None,
                 *, clock=time.monotonic, silence_after_s: float = 30.0,
                 settle_timeout: float = 10.0):
        if type(supervisor) is not ChildSupervisor:
            raise ValueError('lifecycle_supervisor_invalid')
        if broker is not None and type(broker) is not QuestionBroker:
            raise ValueError('lifecycle_broker_invalid')
        if type(silence_after_s) not in (int, float) or silence_after_s <= 0:
            raise ValueError('lifecycle_silence_threshold_invalid')
        self._sup = supervisor
        self._broker = broker
        self._clock = clock
        self._silence = float(silence_after_s)
        self._settle_timeout = float(settle_timeout)
        self._consumed = set()

    # ---- B46.01 取消树 -----------------------------------------------------------

    async def cancel_tree(self) -> dict:
        sup = self._sup
        sup.close_dispatch()                              # ① 先停止新派发
        expired = 0
        if self._broker is not None:
            for rec in sup._children.values():
                if rec.detached:
                    self._broker.transfer_owner(rec.envelope.task_id, rec.envelope.step_id, rec.owner)
            expired = self._broker.cancel_task(sup._scheduler.parent.task_id)
        cancelled = await sup.cancel()                    # ② 传播取消（独立子项不连带）+ 等落定
        results = await sup.wait(timeout=self._settle_timeout)
        states = {s['child_id']: s for s in sup.status()}
        unsettled = [cid for cid, s in states.items()     # ③ 核实：非独立子项必须全部落定
                     if s['state'] == 'running' and not s['detached']]
        if unsettled:
            raise RuntimeError(f'lifecycle_cancel_tree_unsettled:{unsettled}')   # 不谎报终态
        return {'dispatch_closed': not sup.dispatch_open, 'cancelled': cancelled,
                'settled': {cid: results[cid].status for cid in results},
                'detached_running': [cid for cid, s in states.items()
                                     if s['detached'] and s['state'] == 'running'],
                'questions_expired': expired}

    def detach(self, child_id: str, *, owner: str) -> None:
        self._sup.detach(child_id, owner=owner)
        if self._broker is not None:
            env = self._sup._children[child_id].envelope
            self._broker.transfer_owner(env.task_id, env.step_id, owner)

    # ---- B46.03 watchdog / 可重试判定 / 父级判定 ----------------------------------

    def watchdog(self) -> tuple[dict, ...]:
        now = self._clock()
        out = []
        for s in self._sup.status():
            if s['state'] != 'running':
                continue
            last = s['last_progress'] if s['last_progress'] is not None else s['admitted_at']
            age = now - last
            out.append({'child_id': s['child_id'], 'step_id': s['step_id'],
                        'verdict': 'waiting' if age <= self._silence else 'unresponsive',
                        'silent_for_s': round(age, 3)})
        return tuple(out)

    def retryable(self, child_id: str) -> bool:
        rec = self._sup._children.get(child_id)
        if rec is None:
            raise ValueError('worker_child_unknown')
        if rec.result is None or rec.result.status not in ('failed', 'incomplete'):
            return False
        return any(e.type == 'error' and e.payload.get('retryable') is True
                   and e.payload.get('category') in _RETRYABLE_CATEGORIES
                   for e in rec.events)

    @staticmethod
    def parent_verdict(results: dict) -> str:
        """父任务判定：全部 completed 才 completed（一子成功不算完成）。"""
        if not results:
            raise ValueError('lifecycle_verdict_empty')
        statuses = [r.status for r in results.values()]
        if all(s == 'completed' for s in statuses):
            return 'completed'
        if any(s == 'completed' for s in statuses):
            return 'partial'
        return 'failed'

    # ---- B46.02 问题聚合接入 --------------------------------------------------------

    def collect_questions(self, results: dict | None = None) -> tuple:
        if self._broker is None:
            raise ValueError('lifecycle_broker_missing')
        if results is None:
            results = {cid: rec.result for cid, rec in self._sup._children.items()
                       if rec.result is not None}
        created = []
        for cid, result in results.items():
            rec = self._sup._children[cid]
            if (rec.envelope is None or result is None or cid in self._consumed
                    or (not self._sup.dispatch_open and not rec.detached)):
                continue
            self._consumed.add(cid)
            for blocker in result.blockers:
                if blocker.startswith(NEEDS_INPUT_PREFIX):
                    question, is_new = self._broker.emit(rec.envelope,
                                                         blocker[len(NEEDS_INPUT_PREFIX):])
                    if is_new:
                        created.append(question)
        return tuple(created)
