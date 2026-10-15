"""Human command routing. No tools/Skill can authorize or mint a receipt."""
import json

from core.budget_topup.config import load_topup
from core.budget_topup.service import text_report
from core.budget_topup.single import SingleTopup


def config(coord):
    return load_topup(getattr(getattr(coord, '_agent_def', None), 'raw', {}))


def adapter(coord, mode):
    cfg = config(coord)
    if not cfg.enabled:
        raise ValueError('预算续加已关闭（budget_topup.enabled=false）')
    tasks, session = coord.task_service, coord.session_id
    if mode == 'single':
        return SingleTopup(tasks, session, cfg)
    if mode != 'worker':
        raise ValueError('budget_account_mode_invalid')
    from core.orchestration.config import load_orchestration
    from core.orchestration.contracts import Budget
    from core.orchestration.leases import LeaseService
    from memory.repositories.worker_leases import has_account
    task = tasks.current(session)
    if not has_account(tasks.store, task['task_id']):
        raise ValueError('当前 Task 无持久多 Agent 账户；单 Agent 请用 /task budget')
    row = tasks.store._conn.execute('SELECT limits FROM worker_lease_accounts WHERE task_id=?',
                                    (task['task_id'],)).fetchone()
    plans = tasks.child_plans(session, load_orchestration(coord._agent_def.raw))
    return LeaseService(plans, Budget(**json.loads(row[0]))).budget_topup()


def budget_command(coord, tail, source):
    parts = tail.split()
    if parts in (['audit'], ['audit', 'single'], ['audit', 'worker']):
        from memory.repositories.budget_topups import exists
        task = coord.task_service.current(coord.session_id)
        key = (parts[1] if len(parts) == 2 else 'single') + ':' + task['task_id']
        if not exists(coord._store, key):
            return {'account_id': key, 'audit': []}
        rows = coord._store._conn.execute('SELECT body FROM budget_topup_receipts WHERE account_id=? ORDER BY ordinal',
                                          (key,)).fetchall()
        return {'account_id': key, 'audit': [json.loads(r[0]) for r in rows]}
    if not parts or parts in (['single'], ['worker']):
        return text_report(adapter(coord, parts[0] if parts else 'single').status())
    if len(parts) != 3 or parts[0] != 'approve':
        raise ValueError('/task budget [single|worker] 查看进度和逐次授权命令')
    mode, _, task_id = parts[1].partition(':')
    item = adapter(coord, mode)
    if task_id != item.task_id or parts[1] != item.account_id:
        raise ValueError('budget_authorization_wrong_task')
    receipt = item.approve(source(), parts[2])
    return {'message': '本次加额已入账；请普通输入继续已选步骤（不会自动执行）。', 'receipt': receipt}
