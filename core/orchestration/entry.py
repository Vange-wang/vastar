"""Opt-in REPL host wiring of the existing plan/scheduler/supervisor pipeline."""
import asyncio
import json
from dataclasses import replace

from core.harness.task_commands import checkpoint_command
from core.orchestration.budget import BudgetPool
from core.orchestration.children import ChildJob, ChildSupervisor
from core.orchestration.entry_plan import build_plan, proposal
from core.orchestration.entry_publish import accept_child, publish
from core.orchestration.entry_run import begin, finish, identity_guard
from core.orchestration.scheduler import ParentAuthority, Scheduler


def _prepare(coord, config, raw):
    tools = coord.task_tools_context
    task = coord.task_service.current(coord.session_id)
    steps = [s for s in task['steps'] if s['status'] in {'RUNNING', 'VERIFYING'}]
    if len(steps) != 1:
        raise ValueError('请先 /task step 选择唯一执行步骤')
    items = proposal(json.loads(raw), config, tools, task, steps[0])
    from core.llm.pricing import require_pricing
    spec = coord._primary
    require_pricing(spec.pricing, spec.name, spec.model)
    plans = coord.task_service.child_plans(coord.session_id, config)
    if coord._store._conn.execute('SELECT 1 FROM worker_plan_heads WHERE task_id=?',
                                  (task['task_id'],)).fetchone():
        raise ValueError('team_existing_plan: inspect /team; automatic replay is forbidden')
    return items, plans


def _scheduler(coord, config, task, step, run_id, limit, items, plans):
    from core.orchestration.workspaces import WorkspaceManager
    tools = coord.task_tools_context
    manager = None
    if any(i['write'] for i in items):
        manager = WorkspaceManager(tools.project_root, owner=run_id, protected_paths=tools.protected_paths)
    base = manager.base_revision if manager else f"task:{task['task_id']}:{task['revision']}"
    plan, profile, context = build_plan(items, task, step, config, limit, base)
    parent = ParentAuthority(task['task_id'], step['step_id'], base, 1, 1,
                             profile.permissions, tuple(context._evidence))
    provider = replace(coord._primary, params={**coord._primary.params,
        'max_tokens': min(int(coord._primary.params.get('max_tokens') or 2048), 2048)},
        profile=replace(coord._primary.profile,
                        reserve_output_tokens=min(coord._primary.profile.reserve_output_tokens, 2048)))
    scheduler = Scheduler(config=config, parent=parent, sandbox=profile.permissions,
        budget_pool=BudgetPool(limit), tools=tools, provider=provider, store=context,
        plans=plans, workspaces=manager, identity_check=identity_guard(coord, task, run_id))
    for envelope in plan.steps:
        scheduler._validate(envelope, profile)
    plans.create(plan)
    return scheduler, plan, profile


async def run_team(coord, raw, note):
    config = getattr(coord.task_tools_context, 'team_entry_config', None)
    if config is None or not config.enabled:
        raise ValueError('编排未开启；继续使用 /task 的单 Agent 链')
    if coord.has_active_or_pending():
        raise ValueError('请等待当前 Run 和队列结束')
    items, plans = _prepare(coord, config, raw)
    task, step, run_id, limit = begin(coord, config, raw)
    scheduler = supervisor = None
    result = {'status': 'incomplete', 'run_id': run_id}
    try:
        scheduler, plan, profile = _scheduler(coord, config, task, step, run_id, limit, items, plans)
        supervisor = ChildSupervisor(scheduler)
        coord._team_cancel = asyncio.Event()
        supervisor.delegate(tuple(ChildJob(plan, profile, e.step_id, accept_child(item))
                                  for e, item in zip(plan.steps, items, strict=True)))
        note(f'并行计划 {run_id}：配置上限 {config.max_workers}，子任务 {len(plan.steps)}，共享调用预算 {limit.max_calls}')
        await _wait(supervisor, coord._team_cancel, note)
        results = await supervisor.wait()
        if any(value.status != 'completed' for value in results.values()):
            result.update(children={key: value.status for key, value in results.items()},
                          blockers={key: value.blockers for key, value in results.items()},
                          note='宿主任务未完成；保留子任务记录，未发布成果')
        else:
            result = publish(coord, scheduler, items, results, run_id, supervisor.status())
    except (ValueError, OSError, KeyError, TypeError) as exc:
        result.update(reason=str(exc), note='宿主任务未完成；请检查 /team 与 /task status')
    finally:
        if supervisor is not None:
            supervisor.close_dispatch()
            await supervisor.cancel()
        coord._team_cancel = None
        finish(coord, run_id, scheduler, result)
    return checkpoint_command(coord, result)


async def _wait(supervisor, cancelled, note):
    previous = None
    while True:
        rows = supervisor.status()
        signature = [(r['child_id'], r['state'], r['events']) for r in rows]
        if signature != previous:
            for row in rows:
                note(f"子任务 {row['step_id']} · {row['state']} · Run={row['run_id']} · 事件={row['events']}")
            previous = signature
        if not any(row['state'] == 'running' for row in rows):
            return
        if cancelled.is_set():
            supervisor.close_dispatch()
            await supervisor.cancel()
            note('已取消并等待子任务与工具停止；父任务未完成')
            return
        await supervisor.wait(timeout=0.25)
