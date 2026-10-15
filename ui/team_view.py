"""B50.01 只读视图：team/member/task 紧凑列表（依赖/活跃 owner/lease/预算/memory 版本/卡点）。

只读（同 status_view 纪律）：原始只读 SQL，无任何写副作用、不落库、不过期写。
opt-in 扩展表（teams/worker_plan_heads/management_audit 等）不存在时降级为提示，不报错。
"""
import json
import time


def _clip(text, n=60):
    text = str(text)
    return text if len(text) <= n else text[:n] + '…'


def _has(store, table):
    return store._conn.execute(
        'SELECT 1 FROM sqlite_master WHERE name=?', (table,)).fetchone() is not None


def _docs(store, table):
    with store._lock:
        return [json.loads(r[0]) for r in
                store._conn.execute(f'SELECT document FROM {table} ORDER BY rowid')]


def memory_revision(store):
    """全局 memory 版本（memory_revisions 最大修订号）。"""
    with store._lock:
        return store._conn.execute(
            'SELECT COALESCE(MAX(revision),0) FROM memory_revisions').fetchone()[0]


def gate_state(store, task_id):
    if not _has(store, 'worker_dispatch_state'):
        return None
    row = store._conn.execute(
        'SELECT closed FROM worker_dispatch_state WHERE task_id=?', (task_id,)).fetchone()
    return None if row is None else bool(row[0])


def _teams_lines(store):
    if not _has(store, 'teams'):
        return ['团队：无（编排扩展未建）']
    teams, members = _docs(store, 'teams'), _docs(store, 'team_members')
    assigns = _docs(store, 'team_assignments')
    if not teams:
        return ['团队：无']
    lines = []
    for team in teams:
        roster = [m for m in members if m['team_id'] == team['team_id']]
        lines.append(f"团队 {team['team_id']} · owner {team['owner']} · "
                     f"根任务 {_clip(team['root_task'], 24)} · 成员 {len(roster)}")
        for m in roster:
            lines.append('  ' + _member_line(store, m, assigns))
    return lines


def _member_line(store, member, assigns):
    online = (member['status'] == 'active' and member['runtime_id'] is not None
              and member['runtime_epoch'] == store._owner.owner_id)
    current = [a for a in assigns
               if a['member_id'] == member['member_id'] and a['status'] == 'assigned']
    where = '；'.join(f"指派 {_clip(a['task'], 16)}/{a['step_id']} g{a['generation']}"
                      for a in current) or '无在途指派'
    return (f"成员 {member['member_id']} [{member['status']}{'·在线' if online else '·离线'}] "
            f"gen {member['generation']} · 能力档 v{member['profile_version']} · {where}")


def _lease_map(store, task_id):
    if not _has(store, 'worker_leases'):
        return {}
    with store._lock:
        rows = store._conn.execute(
            "SELECT step_id, owner, generation, expires FROM worker_leases "
            "WHERE task_id=? AND state='active'", (task_id,)).fetchall()
    return {r['step_id']: dict(r) for r in rows}


def _budget_text(store, task_id):
    if not _has(store, 'worker_lease_accounts'):
        return None
    row = store._conn.execute(
        'SELECT limits, spent_calls, spent_tokens FROM worker_lease_accounts WHERE task_id=?',
        (task_id,)).fetchone()
    if row is None:
        return None
    limits = json.loads(row['limits'])
    return f"预算 calls {row['spent_calls']}/{limits['max_calls']} · tokens {row['spent_tokens']}/{limits['max_estimated_tokens']}"


def _blockers(store, task_id, plan, records, leases, now):
    """卡点清单：恢复异常 / 待答问题 / 暂停 / 过期 lease / 依赖未验收 / 待审提案 / 记忆冲突。"""
    out = []
    row = store._conn.execute(
        'SELECT recovery_error FROM task_runtime WHERE task_id=?', (task_id,)).fetchone()
    if row and row['recovery_error']:
        out.append(f"恢复需核实：{_clip(row['recovery_error'], 40)}")
    waiting = store._conn.execute(
        "SELECT COUNT(*) FROM task_questions WHERE task_id=? AND state='pending' AND expires_at>?",
        (task_id, time.time())).fetchone()[0]
    if waiting:
        out.append(f'待答问题 {waiting}')
    if gate_state(store, task_id):
        out.append('已暂停（派发闸关闭）')
    stale = sum(1 for lease in leases.values() if lease['expires'] <= now)
    if stale:
        out.append(f'过期未收 lease {stale}')
    deps = {s['step_id']: s['depends_on'] for s in plan['steps']}
    blocked = [k for k, r in records.items() if r['state'] == 'queued'
               and any(records.get(d, {}).get('state') != 'accepted' for d in deps.get(k, []))]
    if blocked:
        out.append(f"等待依赖 {len(blocked)}（{', '.join(blocked)}）")
    if _has(store, 'agent_proposals'):
        pending = store._conn.execute(
            "SELECT COUNT(*) FROM agent_proposals WHERE task_id=? AND status='pending'",
            (task_id,)).fetchone()[0]
        if pending:
            out.append(f'待审共享提案 {pending}')
    if _has(store, 'memory_conflicts'):
        conflicts = store._conn.execute('SELECT COUNT(*) FROM memory_conflicts').fetchone()[0]
        if conflicts:
            out.append(f'记忆冲突 {conflicts}')
    return out


def _step_line(step, record, assignment, lease, now):
    deps = ','.join(step['depends_on']) or '-'
    parts = [f"步骤 {step['step_id']} [{record['state']}] 依赖 {deps}"]
    if assignment:
        parts.append(f"owner {assignment['member_id']} g{assignment['generation']}")
    if lease:
        left = max(0, int(lease['expires'] - now))
        parts.append(f"lease {lease['owner'][:8]} g{lease['generation']} 剩 {left}s")
    if record.get('attempts'):
        parts.append(f"尝试 {record['attempts']}")
    return ' · '.join(parts)


def _task_entry(store, task_id, now):
    head = store._conn.execute(
        'SELECT t.status, t.goal, h.revision FROM worker_plan_heads h '
        'JOIN tasks t ON t.task_id=h.task_id WHERE h.task_id=?', (task_id,)).fetchone()
    value = json.loads(store._conn.execute(
        'SELECT document FROM worker_plan_heads WHERE task_id=?', (task_id,)).fetchone()[0])
    plan, records = value['plan'], value['records']
    assigns = {a['step_id']: a for a in _docs(store, 'team_assignments')
               if a['task'] == task_id} if _has(store, 'team_assignments') else {}
    leases = _lease_map(store, task_id)
    gate = gate_state(store, task_id)
    gate_text = '派发闸 关闭（已暂停）' if gate else '派发闸 开放'
    budget = _budget_text(store, task_id)
    header = (f"任务 {task_id} [{head['status']}] 计划 r{head['revision']} · {gate_text}"
              + (f" · {budget}" if budget else ''))
    lines = [header, f"  目标：{_clip(head['goal'])}"]
    for step in plan['steps']:
        lines.append('  ' + _step_line(step, records[step['step_id']],
                                       assigns.get(step['step_id']),
                                       leases.get(step['step_id']), now))
    blockers = _blockers(store, task_id, plan, records, leases, now)
    if blockers:
        lines.append('  卡点：' + '；'.join(blockers))
    return lines


def _planned_task_ids(store):
    if not _has(store, 'worker_plan_heads'):
        return []
    with store._lock:
        return [r[0] for r in store._conn.execute(
            'SELECT task_id FROM worker_plan_heads ORDER BY rowid')]


def overview(store, *, now=None):
    """团队 + 编排任务总览（B50.01 主视图）。"""
    now = time.time() if now is None else now
    lines = [f'团队与任务总览 · memory 版本 r{memory_revision(store)}']
    lines.extend(_teams_lines(store))
    ids = _planned_task_ids(store)
    if not ids:
        lines.append('编排任务：无（无持久计划；单任务见 /task status、/trace unfinished）')
    for task_id in ids:
        lines.extend(_task_entry(store, task_id, now))
    return '\n'.join(lines)


def task_listing(store, *, now=None):
    now = time.time() if now is None else now
    ids = _planned_task_ids(store)
    if not ids:
        return '编排任务清单：无'
    lines = [f'编排任务清单（{len(ids)} 项 · memory 版本 r{memory_revision(store)}）：']
    for task_id in ids:
        lines.extend(_task_entry(store, task_id, now))
    return '\n'.join(lines)


def task_detail(store, task_id, *, now=None):
    now = time.time() if now is None else now
    if not _has(store, 'worker_plan_heads') or not store._conn.execute(
            'SELECT 1 FROM worker_plan_heads WHERE task_id=?', (task_id,)).fetchone():
        raise ValueError(f'无编排计划的任务：{task_id}（单任务视图用 /trace task）')
    lines = _task_entry(store, task_id, now)
    if _has(store, 'management_audit'):
        rows = store._conn.execute(
            'SELECT ts, actor, action, result FROM management_audit WHERE task_id=? '
            'ORDER BY rowid DESC LIMIT 5', (task_id,)).fetchall()
        if rows:
            lines.append('  最近管理动作：')
            for r in reversed(rows):
                lines.append(f"    {time.strftime('%m-%d %H:%M:%S', time.localtime(r['ts']))} "
                             f"{r['actor']} {r['action']} → {r['result']}")
    return '\n'.join(lines)


def audit_listing(store, task_id=None, *, limit=20):
    if not _has(store, 'management_audit'):
        return '管理动作审计：空（尚无管理动作）'
    if task_id:
        rows = store._conn.execute(
            'SELECT * FROM management_audit WHERE task_id=? ORDER BY rowid DESC LIMIT ?',
            (task_id, limit)).fetchall()
    else:
        rows = store._conn.execute(
            'SELECT * FROM management_audit ORDER BY rowid DESC LIMIT ?', (limit,)).fetchall()
    if not rows:
        return '管理动作审计：空（尚无管理动作）'
    lines = [f"管理动作审计（最近 {len(rows)} 条，时间正序）："]
    for r in reversed(rows):
        step = f"/{r['step_id']}" if r['step_id'] else ''
        target = f" → {r['target']}" if r['target'] else ''
        lines.append(f"  {time.strftime('%m-%d %H:%M:%S', time.localtime(r['ts']))} "
                     f"{r['actor']} {r['action']} {r['task_id']}{step}{target} → {r['result']}")
    return '\n'.join(lines)
