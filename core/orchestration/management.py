"""B50.02 团队/任务管理服务：暂停、恢复（先核账）、重派（提 generation）、取消与审计。

UI 不直改 DB；所有管理动作经 ``TaskService.management(...)`` 进入本服务（任务单 B50.02
「管理动作走服务层」的唯一入口）。每个动作（含被拒）都落 ``management_audit`` 审计。

语义定义（各自明确，互不混用）：

- ``pause``   暂停 = 关派发闸（``worker_dispatch_state`` closed=1、generation+1）：
  在途 lease 不动（自然落定或 TTL 过期由宿主回收），新 claim 一律 ``worker_dispatch_closed``。幂等。
- ``resume``  恢复 = **先核账**（恢复异常 / 未结算 Run 预算 / 未落定操作 reconcile 出 unknown
  三项任一对不上 → **拒绝恢复并说明**），账齐才开闸（closed=0、generation+1）。幂等。
- ``reassign`` 重派 = 仅静默步骤（计划记录非 running 且无未结清 lease）；同团队、目标成员 active；
  **assignment generation 严格 +1**，旧在途结果由 lease/计划记录校验拒绝（不覆盖新事实）。
  能力/预算适配校验在下次派发准入时由 ``Scheduler._validate`` 复核（管理面不持有运行时 Scheduler）。
- ``cancel``  取消 = 关闸 + 在途 lease 真结清（未知用量按全额预留计，同 ``worker_leases`` 口径）
  + 在途步骤记录 → cancelled + 任务 → CANCELLED。只动指定任务，兄弟任务不受影响。

事务纪律：先读验证（失败经 ``_reject`` 独立事务留痕后抛），后单事务写动作+审计；
不在写事务内走拒绝路径（回滚会连审计一起丢）。
"""
import json
import time
from types import SimpleNamespace

from core.orchestration.config import OrchestrationConfig
from core.orchestration.contracts import Budget, nonempty
from core.orchestration.registry import AgentRegistry
from memory.repositories.management_audit import ManagementAuditRepository
from memory.repositories.team_state import TeamState
from memory.repositories.worker_leases import has_account
from memory.repositories.worker_plans import WorkerPlanRepository

TERMINAL = ('COMPLETED', 'CANCELLED')


class _ManagementPlans:
    """LeaseService 需要的 PlanService 表面：管理路径按 task_id 直连，不经会话绑定。"""

    def __init__(self, store, task_id, config, clock):
        self.tasks = SimpleNamespace(store=store)
        self.task_id, self.config, self.clock = task_id, config, clock
        self.repo = WorkerPlanRepository(store)

    def _guard(self):
        if not self.config.enabled:
            raise ValueError('worker_orchestration_disabled')
        task = self.tasks.store.task_get(self.task_id)
        if task['status'] in {'COMPLETED', 'CANCELLED', 'BLOCKED'}:
            raise ValueError('dag_parent_task_unavailable')

    def snapshot(self):
        self._guard()
        return self.repo.read(self.task_id)

    def _load(self, expected):
        value = self.snapshot()
        if type(expected) is not int or value['plan']['revision'] != expected:
            raise ValueError('dag_cas_conflict')
        return value


class ManagementService:
    """任务级管理动作的唯一写入口；actor 记入每条审计（UI 入口 actor='user'）。"""

    def __init__(self, task_service, config, *, actor, clock=time.time):
        if type(config) is not OrchestrationConfig:
            raise ValueError('worker_scheduler_config_invalid')
        nonempty(actor, 'management_actor')
        self.tasks, self.config, self.actor, self.clock = task_service, config, actor, clock
        self.store = task_service.store
        self.audit = ManagementAuditRepository(self.store)
        self.teams = TeamState(self.store)
        self.plans = WorkerPlanRepository(self.store)
        with self.store.transaction():
            from core.orchestration.dispatch_gate import ensure
            ensure(self.store._conn)

    # ---- 公共校验与审计（读相；失败独立事务留痕） --------------------------

    def _reject(self, action, reason, task_id, **kw):
        with self.store.transaction():
            self.audit.record(action=action, task_id=task_id, actor=self.actor, ts=self.clock(),
                              result='rejected', detail={'reason': reason}, **kw)
        raise ValueError(reason)

    def _check_open(self, action, task_id, **kw):
        nonempty(task_id, 'task_id')
        try:
            task = self.store.task_get(task_id)
        except ValueError:
            self._reject(action, f'任务不存在：{task_id}', task_id, **kw)
        if task['status'] in TERMINAL:
            self._reject(action, f'任务已是终态 {task["status"]}，不再接受管理动作', task_id, **kw)
        return task

    def _gate(self, task_id):
        row = self.store._conn.execute(
            'SELECT closed, generation FROM worker_dispatch_state WHERE task_id=?',
            (task_id,)).fetchone()
        return (False, 0) if row is None else (bool(row[0]), row[1])

    def _set_gate(self, task_id, closed):
        self.store._conn.execute(
            'INSERT INTO worker_dispatch_state VALUES(?,?,1) ON CONFLICT(task_id) '
            'DO UPDATE SET closed=?, generation=generation+1', (task_id, int(closed), int(closed)))

    # ---- 暂停 / 恢复 -----------------------------------------------------

    def pause(self, task_id, *, reason=''):
        self._check_open('pause', task_id)
        with self.store.transaction():
            closed, generation = self._gate(task_id)
            if not closed:
                self._set_gate(task_id, True)
            detail = {'reason': reason, 'already_paused': closed,
                      'note': '在途 lease 不受影响；新派发一律拒绝'}
            self.audit.record(action='pause', task_id=task_id, actor=self.actor,
                              ts=self.clock(), result='ok', detail=detail)
        return {'ok': True, 'task_id': task_id, 'paused': True, 'already_paused': closed,
                'gate_generation': generation + (0 if closed else 1)}

    def resume(self, task_id):
        task = self._check_open('resume', task_id)
        with self.store.transaction():
            checks = self._reconcile(task_id)   # reconcile 结论独立落库，不随后续回滚
        if checks['problems']:
            self._reject('resume', '恢复先核账未通过：' + '；'.join(checks['problems']), task_id)
        with self.store.transaction():
            closed, generation = self._gate(task_id)
            if closed:
                self._set_gate(task_id, False)
            detail = dict(checks, was_paused=closed)
            self.audit.record(action='resume', task_id=task_id, actor=self.actor,
                              ts=self.clock(), result='ok', detail=detail)
        return {'ok': True, 'task_id': task_id, 'resumed': closed, 'status': task['status'],
                'gate_generation': generation + (1 if closed else 0), 'checks': checks}

    def _reconcile(self, task_id):
        """核账（恢复前置）：恢复异常 / 未结算 Run 预算 / 未落定操作 unknown 三项。调用者持事务。"""
        problems = []
        row = self.store._conn.execute(
            'SELECT recovery_error, active_run FROM task_runtime WHERE task_id=?', (task_id,)).fetchone()
        recovery_error = row['recovery_error'] if row else None
        active_run = row['active_run'] if row else None
        if recovery_error:
            problems.append(f'恢复需核实：{recovery_error}')
        if active_run:
            problems.append('Run 预算尚未结算，禁止重置')
        pending = [r[0] for r in self.store._conn.execute(
            "SELECT operation_id FROM tool_operations WHERE task_id=? "
            "AND state IN ('prepared','executing','unknown') ORDER BY rowid", (task_id,))]
        operations = {}
        if pending:
            from core.harness.operations import OperationLedger
            ledger = OperationLedger(self.store)
            for operation_id in pending:
                try:
                    operations[operation_id] = ledger.reconcile(operation_id)['status']
                except ValueError as exc:
                    operations[operation_id] = 'error'
                    problems.append(f'未落定操作 {operation_id} 核实异常：{exc}')
        if any(status == 'unknown' for status in operations.values()):
            problems.append('存在核实为 unknown 的未落定操作；禁止重试外部提交')
        return {'recovery_error': recovery_error, 'active_run': active_run,
                'operations': operations, 'problems': problems}

    # ---- 重派 / 取消 -----------------------------------------------------

    def reassign(self, task_id, step_id, target, *, reason=''):
        nonempty(step_id, 'step_id')
        nonempty(target, 'target_member')
        try:
            with self.store.transaction():
                return self._reassign(task_id, step_id, target, reason)
        except ValueError as exc:
            self._reject('reassign', str(exc), task_id, step_id=step_id, target=target)

    def _reassign(self, task_id, step_id, target, reason):
        self._check_open('reassign', task_id, step_id=step_id, target=target)
        key = AgentRegistry.key(task_id, step_id)
        assignment = self._team_record('team_assignments', key, task_id,
                                       f'该步骤无团队指派记录：{step_id}', step_id, target)
        member = self._team_record('team_members', target, task_id,
                                   f'目标成员不存在：{target}', step_id, target)
        self._check_member(assignment, member, task_id, step_id, target)
        value = self._plan_quiescent(task_id, step_id)
        old_generation = assignment['generation']
        assignment.setdefault('history', []).append(
            dict(member_id=assignment['member_id'], profile_version=assignment['profile_version']))
        assignment.update(member_id=target, generation=old_generation + 1,
                          profile_version=member['profile_version'], status='assigned')
        self.teams.put('team_assignments', key, assignment)
        detail = {'reason': reason, 'from': assignment['history'][-1]['member_id'],
                  'to': target, 'old_generation': old_generation,
                  'new_generation': old_generation + 1,
                  'note': '旧在途结果由 lease/计划记录校验拒绝；能力适配在下次派发准入复验'}
        self.audit.record(action='reassign', task_id=task_id, step_id=step_id, target=target,
                          actor=self.actor, ts=self.clock(), result='ok', detail=detail)
        return {'ok': True, 'task_id': task_id, 'step_id': step_id, 'member_id': target,
                'generation': old_generation + 1, 'plan_revision': value['plan']['revision']}

    def _plan_quiescent(self, task_id, step_id):
        try:
            value = self.plans.read(task_id)
        except ValueError:
            self._reject('reassign', f'任务无编排计划：{task_id}', task_id, step_id=step_id)
        record = value['records'].get(step_id)
        if record is None:
            self._reject('reassign', f'步骤不在计划中：{step_id}', task_id, step_id=step_id)
        if record['state'] == 'running':
            self._reject('reassign', '重派须静默：步骤在途运行中；请先暂停/取消或等其落定',
                         task_id, step_id=step_id)
        unsettled = self.store._conn.execute(
            'SELECT lease_id FROM worker_leases WHERE task_id=? AND step_id=? AND settled=0',
            (task_id, step_id)).fetchone()
        if unsettled:
            self._reject('reassign', '存在未结清 lease；先由宿主回收（过期恢复）再重派',
                         task_id, step_id=step_id)
        return value

    def _team_record(self, table, key, task_id, missing, step_id, target):
        try:
            return self.teams.get(table, key)
        except ValueError:
            self._reject('reassign', missing, task_id, step_id=step_id, target=target)

    def _check_member(self, assignment, member, task_id, step_id, target):
        problems = []
        if member.get('status') != 'active':
            problems.append(f'目标成员非 active：{target}')
        if member.get('team_id') != assignment['team_id']:
            problems.append('重派不得跨团队')
        if member.get('root_task') != task_id:
            problems.append('目标成员不属于该任务的根任务')
        if assignment['member_id'] == target:
            problems.append('目标与原 owner 相同，无需重派')
        if problems:
            self._reject('reassign', '；'.join(problems), task_id, step_id=step_id, target=target)

    def cancel(self, task_id, *, reason=''):
        self._check_open('cancel', task_id)
        with self.store.transaction():
            closed, _generation = self._gate(task_id)
            if not closed:
                self._set_gate(task_id, True)
            settled = self._cancel_leases(task_id)
            self.store._conn.execute(
                "UPDATE tasks SET status='CANCELLED', revision=revision+1, updated_at=? "
                'WHERE task_id=?', (self.store._now(), task_id))
            detail = {'reason': reason, 'gate_was_closed': closed, 'settled_leases': settled,
                      'note': '只动本任务的闸/lease/步骤记录；兄弟任务不受影响'}
            self.audit.record(action='cancel', task_id=task_id, actor=self.actor,
                              ts=self.clock(), result='ok', detail=detail)
        return {'ok': True, 'task_id': task_id, 'status': 'CANCELLED',
                'settled_leases': settled}

    def _cancel_leases(self, task_id):
        """在途 lease 真取消：全额结清（未知不猜零）+ 关闭 + 步骤记录 cancelled。调用者持事务。"""
        if not has_account(self.store, task_id):
            return []
        from core.orchestration.leases import LeaseService
        limits = json.loads(self.store._conn.execute(
            'SELECT limits FROM worker_lease_accounts WHERE task_id=?', (task_id,)).fetchone()[0])
        leases = LeaseService(_ManagementPlans(self.store, task_id, self.config, self.clock),
                              Budget(**limits))
        leases.recover_expired()   # 先按宿主口径收过期（重入同事务，不提前提交）
        value = leases.plans.repo.read(task_id)
        settled = []
        for row in self.store._conn.execute(
                "SELECT * FROM worker_leases WHERE task_id=? AND state='active'", (task_id,)).fetchall():
            leases.repo.settle(row['lease_id'])                    # 缺用量按全额预留计
            leases.quotas.settle(row['lease_id'], self.clock(), unknown=True)
            leases.repo.close(row['lease_id'], 'cancelled')
            record = value['records'].get(row['step_id'])
            if record and record.get('lease_id') == row['lease_id'] and record['state'] == 'running':
                record['state'] = 'cancelled'
            settled.append(row['lease_id'])
        if settled:
            leases.plans.repo.write(task_id, value, value['plan']['revision'])
        return settled

    # ---- 审计查询 ---------------------------------------------------------

    def audit_trail(self, task_id=None, *, limit=50):
        return self.audit.list(task_id, limit=limit)
