"""S04 read-only input tools; managed outputs do not grant arbitrary writes."""
from core.tools.operation_receipt import ToolOutcome
from core.tools.registry import ToolSpec
from core.tools.sheet_service import SheetService


def install_sheet_tools(context):
    context.sheet_service = SheetService(context)
    for name in ('sheet_capabilities', 'analyze_spreadsheet'):
        analyzing = name == 'analyze_spreadsheet'
        context.registry.register(ToolSpec(name,
            '冻结表格副本，逐次批准离线隔离分析；显式清洗、独立复算、公式不执行，产物分页核查。',
            {'type': 'object', 'properties': {'path': {'type': 'string'}, 'header': {'type': 'boolean'},
                'rules': {'type': 'array', 'items': {'type': 'object'}}, 'units': {'type': 'object'}} if analyzing else {},
             'required': ['path'] if analyzing else [], 'additionalProperties': False},
            'B', lambda **_: 'special handler required', effect='read', resource_scope='file', retryable=False))


def guard(context, spec):
    skills = context.skills
    if (skills is not None and 'spreadsheet-analysis' in skills.active_names()
            and 'spreadsheet-analysis' in skills.available() and spec.effect != 'read'
            and spec.name != 'terminate_process'):
        return ToolOutcome(spec.name, 'rejected', 'S04 不授予任意写入/命令权限', origin='skill_policy')
    return None
