"""Vastar runtime component."""

from __future__ import annotations

from dataclasses import dataclass, replace

from core.errors import ErrorCode
from core.token import check_gate, measure

REF_MARKER = "[大输出引用"   # 大输出引用行标记（preflight 分项识别两口径之一）
FOLD_LAYER = "大输出引用折叠"


@dataclass
class SegmentSelection:
    """片段选择结果：最终消息集 + 计量/闸门/预警 + 生效层清单。"""

    messages: list
    measurement: object
    gate: object
    warnings: list[str]
    layers_executed: list[str]


def fold_large_output_refs(messages: list) -> tuple[list, bool]:
    """把含大输出引用标记的消息折叠为引用行（返回 (新列表, 是否折叠)）。

    只改投影内容、不动存储原文（source_id/source_hash 不变）；折叠后全文可经
    引用行里的 artifact_ref + next_cursor 续读——可追溯，非静默丢弃。
    """
    folded = False
    out = []
    for m in messages:
        if REF_MARKER in m.content:
            lines = [ln for ln in m.content.splitlines() if REF_MARKER in ln]
            out.append(replace(m, content=(
                "\n".join(lines)
                + f"\n[大输出已折叠：原文 {len(m.content)} 字符在产物库，续读见引用行]")))
            folded = True
        else:
            out.append(m)
    return out, folded


def _texts_of(system_text: str, messages: list, current_input: str,
              tool_schema_text: str | None) -> list[str]:
    from memory.history import wire_text
    texts = [system_text] + [wire_text(m) for m in messages] + [current_input]
    if tool_schema_text:
        texts.append(tool_schema_text)
    return texts


def select_and_gate(*, system_text: str, messages: list, current_input: str,
                    tool_schema_text: str | None, profile,
                    safety_margin_ratio: float, strict: bool,
                    layers_executed: list[str], layers_not_executed: list[str],
                    warn_threshold_ratio: float = 0.75) -> SegmentSelection:
    """计量 → 闸门（超预算先试折叠显式层再复量；仍超显式失败）。

    闸门与显式失败口径与拆分前逐字一致（含 details 三键）；预警阈值逻辑不变。
    """
    executed = list(layers_executed)
    measurement = measure(_texts_of(system_text, messages, current_input, tool_schema_text))
    gate = check_gate(measurement, profile,
                      safety_margin_ratio=safety_margin_ratio, strict=strict)
    if not gate.allowed:
        folded, did_fold = fold_large_output_refs(messages)
        if did_fold:
            executed = [*executed, FOLD_LAYER]
            messages = folded
            measurement = measure(
                _texts_of(system_text, messages, current_input, tool_schema_text))
            gate = check_gate(measurement, profile,
                              safety_margin_ratio=safety_margin_ratio, strict=strict)
    if not gate.allowed and gate.error is not None:
        err = gate.error
        if err.code == ErrorCode.CONTEXT_BUDGET_EXCEEDED:
            err.details["layers_executed"] = executed
            err.details["layers_not_executed"] = [
                "静默删除历史", "截短当前输入原文", "降低输出预留",
            ]
            err.details["source_count"] = (len(messages) + 1 + (1 if current_input else 0))
        raise err

    warnings: list[str] = []
    if profile.input_budget > 0:
        ratio = gate.gate_tokens / profile.input_budget
        if ratio >= warn_threshold_ratio:
            warnings.append(
                f"上下文估算已用预算 {ratio:.0%}（预警阈值 {warn_threshold_ratio:.0%}）；"
                "仅估算口径，非硬上界。"
            )
    return SegmentSelection(messages=messages, measurement=measurement, gate=gate,
                            warnings=warnings, layers_executed=executed)
