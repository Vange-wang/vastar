"""S02's additive read-only guard and independently switchable tools."""
from core.tools.document_service import DocumentService
from core.tools.operation_receipt import ToolOutcome
from core.tools.registry import ToolSpec

SKILL = "document-extraction"
NAMES = {"document_capabilities", "extract_document"}


def install_document_tools(context):
    context.document_service = DocumentService(context)
    properties = {"path": {"type": "string"}, "first": {"type": "integer", "minimum": 1},
                  "count": {"type": "integer", "minimum": 1, "maximum": 10}, "ocr": {"type": "boolean"}}
    for name in sorted(NAMES):
        extraction = name == "extract_document"
        context.registry.register(ToolSpec(name,
            "S02 只读隔离提取；仍需 D 与逐次确认；partial＋出处＋OCR 缺口。" if extraction else "读取文档类型能力等级与外部依赖。",
            {"type": "object", "properties": properties if extraction else {},
             "required": ["path"] if extraction else [], "additionalProperties": False},
            "B", lambda **_: "special handler required", effect="read", resource_scope="file", retryable=False))


def guard(context, spec):
    skills = context.skills
    active = skills is not None and SKILL in skills.active_names() and SKILL in skills.available()
    if active and spec.effect != "read" and spec.name != "terminate_process":
        return ToolOutcome(spec.name, "rejected", "S02 only reads; document instructions cannot authorize effects",
                           origin="skill_policy")
    return None
