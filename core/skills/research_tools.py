"""Additive S01 guard/readonly verifier; B11 contracts and B29 services stay unchanged."""
import json

from core.skills.research_sources import EvidenceError
from core.skills.research_verification import verify
from core.tools.operation_receipt import ToolOutcome
from core.tools.operation_runner import thread_call
from core.tools.registry import ToolSpec

SKILL = "research-verification"
VERIFY = "verify_research"
LIMITS = {"web_search": 3, "read_web_page": 6}


def install_research_tool(context):
    context.registry.register(ToolSpec(VERIFY, "S01：以当前 Run 正文回执核验来源/冲突/逐句引用，返回四件式只读报告。",
        {"type": "object", "properties": {"sources": {"type": "array", "maxItems": 12},
                                           "inferences": {"type": "array", "maxItems": 8}},
         "required": ["sources"], "additionalProperties": False}, "A", lambda **_: "special dispatch required",
        effect="read", resource_scope="network", retryable=True, preserve_full_result=True))


def active(context):
    skills = context.skills
    return skills is not None and SKILL in skills.active_names() and SKILL in skills.available()


def guard(context, spec, session_id, run_id):
    if spec.name == VERIFY and not active(context):
        return ToolOutcome(spec.name, "rejected", "S01 未启用/未加载", origin="skill_policy")
    if not active(context):
        return None
    if spec.effect != "read" and spec.name != "terminate_process":
        return ToolOutcome(spec.name, "rejected", "S01 只读；写记忆/扩权/命令须退出技能并走既有确认链",
                           origin="skill_policy")
    if spec.name in LIMITS:
        store = context.operation_ledger.store
        with store._lock:
            count = store._conn.execute("SELECT COUNT(*) FROM tool_operations WHERE run_id=? AND tool=?",
                                        (run_id, spec.name)).fetchone()[0]
        if count >= LIMITS[spec.name]:
            return ToolOutcome(spec.name, "rejected", "S01 检索/读取预算耗尽；停止并报告缺口",
                               origin="research_budget")
    return None


async def execute(context, args, session_id, run_id, cancel):
    if cancel.is_set():
        return ToolOutcome(VERIFY, "cancelled", "核验已取消", origin="user_cancel")
    try:
        report = await thread_call(verify, context, args, session_id, run_id)
        if cancel.is_set():
            return ToolOutcome(VERIFY, "cancelled", "核验已取消", origin="user_cancel")
        return ToolOutcome(VERIFY, "executed", result=json.dumps(report, ensure_ascii=False), redact_result_preview=True)
    except EvidenceError as exc:
        return ToolOutcome(VERIFY, "rejected", str(exc), origin="research_evidence")
    except Exception:
        return ToolOutcome(VERIFY, "failed", "证据依赖无法核实；未编造结论", origin="research_evidence")
