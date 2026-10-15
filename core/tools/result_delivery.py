"""工具结果到事件与模型反馈的单一适配点；保留错误来源和结构化契约。"""
from core.events import EventType
from core.tools.audit import result_fields


async def dispatch_result(emit, s, name, result, attempt_id, attempt_index):
    """取消仅落事件并停止 Run；其余结果回填模型，来源不与事件信封混用。"""
    audit = {**(result.audit or {}), "tool_origin": result.origin,
             "tool_contract": result.contract}
    if result.status == "executed":
        s.tool_exec_count += 1
        result_audit = result_fields(result.result)
        if result.redact_result_preview:
            # E-6：凭据类（.env）读取——内容预览不落审计/库（内容不落库口径）
            result_audit["preview"] = "<已脱敏：凭据类文件内容不落审计>"
        await emit(s, EventType.TOOL_EXECUTED, {
            "tool": name, "duration_ms": result.duration_ms,
            "truncated_bytes": result.truncated_bytes,
            **result_audit, **audit,
            "source": "model",
        }, attempt_id, attempt_index)
        
        # B16：有产物库标记块时用它（含 artifact_ref + next_cursor），否则沿用字节数标记
        if result.truncated_bytes:
            note = (result.audit or {}).get("large_output")
            return result.result + (f"\n{note}" if note
                                    else f"\n[已截断 {result.truncated_bytes} 字节]")
        return result.result
    if result.status == "rejected":
        await emit(s, EventType.TOOL_REJECTED, {
            "tool": name, "reason": result.reason, **audit,
            "source": "model",
        }, attempt_id, attempt_index)
        return f"[工具被拒绝] {result.reason}（来源：{result.origin}）"
    if result.status in ("failed", "unknown", "timeout"):
        await emit(s, EventType.TOOL_FAILED, {
            "tool": name, "reason": result.reason, **audit,
            "source": "model",
        }, attempt_id, attempt_index)
        label = ("工具超时（执行体已停止）" if result.status == "timeout" else
                 "工具结果待核实，禁止盲目重试" if result.status == "unknown" else "工具失败")
        return f"[{label}] {result.reason}（来源：{result.origin}）"
    # cancelled：用户取消 → 既有取消路径语义（状态 cancelled，不写完成态）
    await emit(s, EventType.TOOL_CANCELLED, {
        "tool": name, "source": "model", "reason": result.reason, **audit,
    }, attempt_id, attempt_index)
    return None

