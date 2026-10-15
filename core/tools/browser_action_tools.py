"""Separate default-closed write tools; B34's names and contracts remain readonly."""
from core.tools.browser_actions import BrowserActions
from core.tools.registry import ToolSpec

NAMES = {'browser_click', 'browser_fill', 'browser_reconcile'}


def install(context):
    context.browser_service.actions = BrowserActions(context.browser_service)
    for name in sorted(NAMES):
        properties = {key: {'type': 'string'} for key in
                      ('browser_session_id', 'tab_id', 'page_revision', 'origin', 'target_id')}
        required = list(properties)
        if name == 'browser_fill':
            properties['value_ref'] = {'type': 'string'}
            required.append('value_ref')
        elif name == 'browser_click':
            properties.update(verify={'type': 'object'}, request={'type': 'object'})
            required.append('verify')
        else:
            properties = {key: {'type': 'string'} for key in ('operation_id', 'browser_session_id', 'tab_id')}
            required = list(properties)
        context.registry.register(ToolSpec(name, '受控浏览器动作/只读核实；未知结果禁止重投；写入逐次人类确认。',
            {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False},
            'B' if name == 'browser_reconcile' else 'C', lambda **_: 'special handler required',
            effect='read' if name == 'browser_reconcile' else 'write', resource_scope='browser', retryable=False))
