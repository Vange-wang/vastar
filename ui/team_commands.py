"""B50 /team 命令：可见性视图 + 管理动作入口（动作一律走 ManagementService）。

不全屏接管（B50.01 红线）：沿用紧凑列表 + render_note 呈现；无序号菜单之外的交互形态。
管理动作解析 ManagementService（coord.task_tools_context.team_management）；
未装配（orchestration.enabled=false 或未接线）时动作给降级提示，只读视图仍可用。
"""
from ui.team_view import audit_listing, overview, task_detail, task_listing

HELP = ("/team 团队与任务总览（依赖/owner/lease/预算/memory 版本/卡点） · /team tasks 任务清单 · "
        "/team task <任务ID> 任务明细 · /team pause <任务ID> [原因] 暂停派发（在途不动） · "
        "/team resume <任务ID> 先核账后恢复（对不上账则拒） · "
        "/team reassign <任务ID> <步骤ID> <成员ID> 静默重派（提 generation） · "
        "/team cancel <任务ID> [原因] 取消任务（只影响该任务） · /team audit [任务ID] 管理动作审计。")


def _management(coord):
    ctx = getattr(coord, 'task_tools_context', None)
    return getattr(ctx, 'team_management', None)


def _require_management(coord):
    mgmt = _management(coord)
    if mgmt is None:
        raise ValueError('编排管理未装配（orchestration.enabled=false 或未接线）；'
                         '只读视图仍可用：/team、/team tasks、/team audit')
    return mgmt


def _action(coord, cmd, tail):
    mgmt = _require_management(coord)
    if cmd == 'pause':
        task_id, _, reason = tail.partition(' ')
        result = mgmt.pause(task_id.strip(), reason=reason.strip())
        note = '已暂停（此前已暂停，幂等）' if result['already_paused'] else '已暂停'
        return f"{note}：{result['task_id']} · 派发闸代次 g{result['gate_generation']} · 在途 lease 不动"
    if cmd == 'resume':
        result = mgmt.resume(tail.strip())
        state = '已恢复（开闸）' if result['resumed'] else '账齐，此前未暂停（幂等）'
        return (f"{state}：{result['task_id']} · 核账通过（恢复异常 无 · 未落定操作 "
                f"{len(result['checks']['operations'])} 项均非 unknown · Run 预算已结）")
    if cmd == 'cancel':
        task_id, _, reason = tail.partition(' ')
        result = mgmt.cancel(task_id.strip(), reason=reason.strip())
        return (f"已取消：{result['task_id']} → CANCELLED · 结清在途 lease "
                f"{len(result['settled_leases'])} 个 · 兄弟任务不受影响")
    if cmd == 'reassign':
        parts = tail.split()
        if len(parts) != 3:
            raise ValueError('用法：/team reassign <任务ID> <步骤ID> <成员ID>')
        result = mgmt.reassign(parts[0], parts[1], parts[2])
        return (f"已重派：{result['task_id']}/{result['step_id']} → {result['member_id']} "
                f"· generation 提升为 g{result['generation']} · 旧在途结果将被拒收")
    raise ValueError(HELP)


def handle_team(ui, arg: str) -> None:
    coord = ui._coordinator
    store = getattr(coord, '_store', None)
    if store is None:
        ui._renderer.render_note('/team 不可用：存储未装配')
        return
    cmd, _, tail = arg.partition(' ')
    tail = tail.strip()
    try:
        if cmd == '':
            ui._renderer.render_note(overview(store))
        elif cmd == 'help':
            ui._renderer.render_note(HELP)
        elif cmd == 'tasks':
            ui._renderer.render_note(task_listing(store))
        elif cmd == 'task':
            if not tail:
                raise ValueError('用法：/team task <任务ID>')
            ui._renderer.render_note(task_detail(store, tail))
        elif cmd == 'audit':
            ui._renderer.render_note(audit_listing(store, tail or None))
        elif cmd in ('pause', 'resume', 'cancel', 'reassign'):
            ui._renderer.render_note(_action(coord, cmd, tail))
        else:
            ui._renderer.render_note(f'未知子命令 {cmd}。{HELP}')
    except (ValueError, KeyError, TypeError) as exc:
        ui._renderer.render_note(f'/team {cmd or "?"}：{exc}')
