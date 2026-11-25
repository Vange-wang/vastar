"""S03 only creates owned deliverables; no arbitrary write target or permission grant."""
from core.tools.operation_receipt import ToolOutcome
from core.tools.registry import ToolSpec
from core.tools.report_service import ReportService


def install_report_tools(context):
    context.report_service = ReportService(context)
    column = {'type': 'object', 'properties': {'field': {'type': 'string'}, 'label': {'type': 'string'}},
              'required': ['field', 'label'], 'additionalProperties': False}
    table = {'type': 'object', 'description': '原始 JSON 对象数组的表格投影。按原顺序保留所选字段。',
             'properties': {'title': {'type': 'string'}, 'columns': {'type': 'array', 'items': column,
                                                                     'minItems': 1, 'maxItems': 5}},
             'required': ['title', 'columns'], 'additionalProperties': False}
    for name in ("report_capabilities", "generate_report"):
        generation = name == "generate_report"
        context.registry.register(ToolSpec(name,
            "S03 只读来源生成受管报告；必须逐次批准离线隔离执行；不覆盖原件，真人版式待用户。",
            {"type": "object", "properties": {"path": {"type": "string"}, 'table': table} if generation else {},
             "required": ["path"] if generation else [], "additionalProperties": False},
            "B", lambda **_: "special handler required", effect="read", resource_scope="file", retryable=False))


def guard(context, spec):
    skills = context.skills
    if (skills is not None and "report-generation" in skills.active_names()
            and "report-generation" in skills.available() and spec.effect != "read"
            and spec.name != "terminate_process"):
        return ToolOutcome(spec.name, "rejected", "S03 只读来源并生成受管产物；不授予其他写入或执行权限",
                           origin="skill_policy")
    return None
