"""Trusted REPL entry only; no model-callable release/approval tool is registered."""
import json

from core.skills.maintenance import SKILL
from core.skills.release_package import require

HELP = ('/skill release candidate <JSON: source_task,memory_key,bundle,private_terms>; '
        'validate <JSON: release_id,change_id> (independent review session); '
        'enable/rollback <JSON: release_id,expected_head>; retire <JSON: release_id>; status. '
        'Bundle contains skill_md, resources, examples, dependencies. Read the exact artifact/hash before enable. '
        'Use an authorized, unexpired project procedure via /remember; scripts remain forbidden.')


def command(coord, arg):
    require(not coord.is_busy(), 'wait_or_cancel_current_run')
    service = getattr(coord, 'skill_maintenance', None)
    require(service is not None, 'maintenance_dependency_missing')
    require(SKILL in service.context.skills.available(), 'maintenance_skill_disabled')
    action, _, tail = arg.partition(' ')
    if action in {'', 'help'}:
        return HELP
    coord.ensure_session()
    with service.store._lock:
        if action == 'status':
            return service.status()
        data = json.loads(tail)
        if action == 'candidate':
            return service.candidate(coord.session_id, **data)
        if action == 'validate':
            return service.validate(coord.session_id, **data)
        if action in {'enable', 'rollback'}:
            return service.publish(coord.session_id, **data, rollback=action == 'rollback')
        if action == 'retire':
            return service.retire(coord.session_id, **data)
        raise ValueError(HELP)


def handle(renderer, coord, arg):
    try:
        result = command(coord, arg)
        renderer.render_note(result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        renderer.render_note('Skill release unavailable: ' + str(exc))
