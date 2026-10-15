"""工具结果与可核对的运行期回执。"""
from dataclasses import dataclass, field
from pathlib import Path

from core.artifacts import large_output_note
from core.tools.sensitive_result import protected_result

MAX_RESULT_BYTES = 64 * 1024

def _friendly_failure_reason(exc: BaseException, resolved: Path | None) -> str:
    """Vastar runtime component."""
    if isinstance(exc, PermissionError):
        return (f"无访问权限：{resolved or '目标路径'}"
                "（该目录可能属于其他账户或受系统保护）")
    if isinstance(exc, FileNotFoundError):
        return f"路径不存在：{resolved}"
    return f"工具实现异常（{type(exc).__name__}）：{exc}"


@dataclass
class ToolOutcome:
    """Vastar runtime component."""

    name: str
    status: str            # executed / rejected / failed / cancelled / timeout / unknown
    reason: str = ""       # rejected/failed 的原因（用户可见 + 审计）
    result: str = ""       # 仅 executed；截断后文本
    duration_ms: int = 0
    truncated_bytes: int = 0
    args_summary: str = ""
    audit: dict = field(default_factory=dict)   
    redact_result_preview: bool = False   # E-6：凭据类读取的结果预览不落审计（内容不落库）
    origin: str = ""       # B10：结果来源（阶段）；executed 为 ""
    contract: dict = field(default_factory=dict)   # B10：read_window / write_proof / recovery


_CONTRACT_FACT_KEYS = ("read_window", "write_proof", "recovery")


def _contract_from_facts(facts: dict) -> dict:
    """从 control.facts 提取结构化契约键（工具 run 写入，此处统一搬到 ToolOutcome.contract）。"""
    return {k: facts[k] for k in _CONTRACT_FACT_KEYS if k in facts}




async def run_sync(spec, args, resolved, project_root, skip, args_sum,
                   resolved_extra, audit_fields, control, *, data_dir=None, artifacts=None):
    from core.tools.operation_control import OperationCancelled
    from core.tools.operation_resources import file_facts
    from core.tools.operation_runner import thread_call
    control.checkpoint("prepare")
    from core.tools.isolated_read import run_isolated, supports
    if supports(spec):
        outcome = await run_isolated(spec, args, resolved, project_root, args_sum, control)
        if outcome.status == 'executed':
            return result_outcome(spec, outcome.result, args_sum, audit_fields, control, artifacts, resolved)
        return outcome
    paths = [resolved, *(resolved_extra or {}).values()]
    control.facts.update(audit_fields or {})
    control.facts["before"] = await thread_call(file_facts, paths)
    if getattr(control, "ledger", None):
        control.ledger.prepare(control)
        control.ledger.executing(control)
    def invoke():
        control.checkpoint("prepare")
        control.started = True
        control.on_started()
        return spec.run(args, resolved_path=resolved, resolved_extra=resolved_extra,
                        project_root=project_root, skip=skip,
                        op_context={"backup_path": (audit_fields or {}).get("backup_path"),
                                    "control": control, "data_dir": data_dir,
                                    "artifacts": artifacts})
    try:
        result = await thread_call(invoke)
    except OperationCancelled:
        raise
    except Exception as exc:
        if control.commit_started or not spec.cooperative and control.started:
            if control.state != "committed":
                control.transition("unknown")
        else:
            control.transition("failed")
        return ToolOutcome(spec.name, control.state, _friendly_failure_reason(exc, resolved),
                           args_summary=args_sum,
                           origin=getattr(exc, "origin", "tool_runtime"),
                           audit={"error_class": type(exc).__name__, "errno": getattr(exc, "errno", None)})
    finally:
        if control.started:
            control.facts["after"] = await thread_call(file_facts, paths)
    return result_outcome(spec, result, args_sum, audit_fields, control, artifacts, resolved)


def result_outcome(spec, result, args_sum, audit_fields, control, artifacts, resolved):
    data = result.encode("utf-8")
    truncated = 0 if spec.preserve_full_result else max(0, len(data) - MAX_RESULT_BYTES)
    audit_out = dict(audit_fields or {})
    if protected_result(spec.name, result):
        audit_out["credential_read"] = True
    if truncated:
        result = data[:MAX_RESULT_BYTES].decode("utf-8", errors="ignore")
        # B16：截断必须显式 + 全量落产物库（凭据类来源按 R25 不落）；禁静默截断
        audit_out["large_output"] = large_output_note(
            artifacts, data, source_ref=str(resolved),
            sensitive=bool(audit_out.get("credential_read")))
    return ToolOutcome(spec.name, "executed", result=result, truncated_bytes=truncated,
                       args_summary=args_sum, audit=audit_out,
                       redact_result_preview=bool(audit_out.get("credential_read")),
                       contract=_contract_from_facts(control.facts))
