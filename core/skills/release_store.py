"""Append-only publication/pin events in the existing OperationLedger; no schema fork."""
from core.skills.release_package import digest, require
from core.tools.operation_control import OperationControl
from core.tools.operation_receipt import ToolOutcome
from core.tools.registry import ToolSpec


def events(service, tool='skill_release'):
    rows = service.store._conn.execute("SELECT operation_id FROM tool_operations WHERE tool=? AND state='committed' ORDER BY rowid", (tool,))
    result = []
    for row in rows:
        facts = service.store.operation_get(row[0])['facts']
        if facts.get('project') == service.project:
            result.append(facts)
    return result


def catalog(service):
    versions, heads = {}, {}
    for event in events(service):
        row = event['release']
        if event['action'] in {'enable', 'rollback'}:
            prior = heads.get(row['name'])
            if prior and prior != row['id']:
                versions[prior] = {**versions[prior], 'state': 'retired'}
            heads[row['name']] = row['id']
        elif event['action'] == 'retire' and heads.get(row['name']) == row['id']:
            heads[row['name']] = None
        versions[row['id']] = row
    return versions, heads


def append(service, action, payload, *, tool='skill_release'):
    require(not service.store._conn.in_transaction, 'release_nested_transaction_refused')
    control = OperationControl()
    spec = ToolSpec(tool, 'trusted human skill lifecycle', {}, 'A', lambda: None, effect='write', retryable=False)
    facts = {'before': {}, 'project': service.project, 'action': action, **payload,
             'permission': 'USER_COMMAND', 'approval_source': 'user_command'}
    service.context.operation_ledger.bind(control, spec, digest(facts))
    control.facts = facts
    service.context.operation_ledger.prepare(control)
    service.context.operation_ledger.executing(control)
    service.context.operation_ledger.finish(control, ToolOutcome(tool, 'executed', result='metadata committed'))
    return control.operation_id


def pins(service, task_id):
    return next((e['pins'] for e in reversed(events(service, 'skill_pin')) if e['task_id'] == task_id), None)
