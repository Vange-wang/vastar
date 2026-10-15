"""Vastar runtime component."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from core.context_segments import select_and_gate
from core.prompt import PromptBundle
from core.token import (
    GUARANTEE_ESTIMATED,
    BudgetProfile,
    GateDecision,
    TokenMeasurement,
    measure,
)


@dataclass
class SourceMessage:
    """会话历史中的来源对象（只增不删；投影只读引用）。"""

    message_id: str
    role: str                    # user / assistant
    content: str
    reasoning_content: str = ""  # 存储与展示用；阶段一不回传给模型
    status: str = "complete"     # complete / partial
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None
    model_call_ids: list[str] = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)

    @property
    def source_hash(self) -> str:
        payload = json.dumps([self.message_id, self.role, self.content, self.tool_calls,
                              self.tool_call_id, self.model_call_ids, self.artifact_refs], ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class ContextSnapshot:
    """模型当时看到了什么（权威记录，落 context_snapshots 表）。"""

    system_text: str
    prompt_version: str
    prompt_hash: str
    messages: list[dict]                       # OpenAI 格式投影（不含 reasoning_content）
    source_ids: list[str]                      # 来源顺序清单（"system" + message_id…）
    source_hashes: list[str]
    estimated_tokens: int
    budget: int
    guarantee: str = GUARANTEE_ESTIMATED

    def to_record(self) -> dict:
        return {
            "prompt_version": self.prompt_version,
            "prompt_hash": self.prompt_hash,
            "system_text": self.system_text,
            "messages": self.messages,
            "source_ids": self.source_ids,
            "source_hashes": self.source_hashes,
            "estimated_tokens": self.estimated_tokens,
            "budget": self.budget,
            "guarantee": self.guarantee,
        }


@dataclass
class ContextResult:
    snapshot: ContextSnapshot
    measurement: TokenMeasurement
    gate: GateDecision
    layers_executed: list[str] = field(default_factory=list)
    layers_not_executed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class CompactSnapshot:
    """Vastar runtime component."""

    snapshot_id: str
    digest_text: str
    covered_message_ids: list[str]
    covered_count: int
    target_ratio: float
    tokens_before: int
    tokens_after: int
    guarantee: str = GUARANTEE_ESTIMATED

    @classmethod
    def from_record(cls, record: dict) -> CompactSnapshot:
        """从 store 压缩快照行（record + snapshot_id）构建。"""
        return cls(
            snapshot_id=record["snapshot_id"],
            digest_text=record["digest_text"],
            covered_message_ids=list(record["covered_message_ids"]),
            covered_count=int(record["covered_count"]),
            target_ratio=float(record["target_ratio"]),
            tokens_before=int(record["tokens_before"]),
            tokens_after=int(record["tokens_after"]),
            guarantee=str(record.get("guarantee", GUARANTEE_ESTIMATED)),
        )


@dataclass
class CompactDigest:
    """/compact 生成的摘要产物（pending，未生效；批准后经 store 持久化并转层②输入）。"""

    digest_text: str
    covered_message_ids: list[str]
    covered_count: int
    target_ratio: float
    tokens_before: int
    tokens_after: int
    guarantee: str = GUARANTEE_ESTIMATED


def _dedup_by_source_id(messages: list[SourceMessage]) -> list[SourceMessage]:
    """第一层：同一来源 ID 重复注入时保留首次出现（仅针对重复注入，不做语义判重）。"""
    seen: set[str] = set()
    out: list[SourceMessage] = []
    for m in messages:
        if m.message_id in seen:
            continue
        seen.add(m.message_id)
        out.append(m)
    return out


MIN_DIGEST_CAP = 12          
DIGEST_HEADROOM_CHARS = 8    


def _render_digest(history: list[SourceMessage], cap: int) -> str:
    """逐条摘录拼装（内容全部来自原文，无改写无幻觉；cap = 每条摘录字符上限）。"""
    lines = ["【会话早期摘要】以下为此前对话的确定性摘录"
             "（/compact 生成；原始全文完整保留于会话库，可回滚）："]
    for m in history:
        who = "用户" if m.role == "user" else "助手"
        content = m.content.strip().replace("\n", " ")
        if len(content) <= cap:
            excerpt = content
        else:
            
            excerpt = content[:cap] + f"…[截断 {len(content) - cap} 字]"
        lines.append(f"- {who}: {excerpt}" if excerpt else f"- {who}:（空）")
    return "\n".join(lines)


def build_compact_digest(history: list[SourceMessage], *,
                         target_ratio: float) -> CompactDigest:
    """Vastar runtime component."""
    texts = [m.content for m in history]
    tokens_before = measure(texts).estimated_tokens
    total_chars = sum(len(t) for t in texts)
    cap = max(MIN_DIGEST_CAP, int(total_chars * target_ratio / max(1, len(history)))
              - DIGEST_HEADROOM_CHARS)
    digest_text = _render_digest(history, cap)
    tokens_after = measure([digest_text]).estimated_tokens
    if tokens_before > 0 and tokens_after > tokens_before * target_ratio and cap > MIN_DIGEST_CAP:
        cap = max(MIN_DIGEST_CAP, cap // 2)
        digest_text = _render_digest(history, cap)
        tokens_after = measure([digest_text]).estimated_tokens
    return CompactDigest(
        digest_text=digest_text, covered_message_ids=[m.message_id for m in history],
        covered_count=len(history), target_ratio=target_ratio,
        tokens_before=tokens_before, tokens_after=tokens_after)


def _apply_compact_layer(deduped: list[SourceMessage],
                         approved_compact: CompactSnapshot | None,
                         ) -> tuple[list[SourceMessage], list[str], list[str]]:
    """层②：生效快照把被覆盖原文替换为摘要消息（置于其余原文之前，保持时序）。

    未生效（pending/ dropped/ rolled_back）或不存在的快照绝不参与装配。
    """
    if approved_compact is None:
        return deduped, ["source_dedup"], [
            "apply_approved_snapshot（无生效快照）", "explicit_user_compact（未使用）"]
    covered = set(approved_compact.covered_message_ids)
    from memory.compaction_groups import validate_coverage
    validate_coverage(deduped, approved_compact.covered_message_ids)
    digest = SourceMessage(
        message_id=f"compact-{approved_compact.snapshot_id}", role="assistant",
        content=approved_compact.digest_text, status="complete")
    remaining = [m for m in deduped if m.message_id not in covered]
    # 固定记忆前缀始终位于摘要之前，避免版本链改变其位置。
    prefix = [m for m in remaining if m.message_id.startswith('memory:')]
    tail = [m for m in remaining if not m.message_id.startswith('memory:')]
    return ([*prefix, digest, *tail],
            ["source_dedup", "apply_approved_snapshot", "explicit_user_compact"], [])


def _project_messages(system_text: str, messages: list[SourceMessage],
                      current_input: str) -> tuple[list[dict], list[str], list[str]]:
    projection: list[dict] = [{"role": "system", "content": system_text}]
    source_ids: list[str] = ["system"]
    source_hashes: list[str] = [
        hashlib.sha256(system_text.encode("utf-8")).hexdigest()
    ]
    for m in messages:
        from memory.history import wire_message
        projection.append(wire_message(m))
        source_ids.append(m.message_id)
        source_hashes.append(m.source_hash)
    if current_input:
        projection.append({"role": "user", "content": current_input})
        source_ids.append("current_input")
        source_hashes.append(
            hashlib.sha256(current_input.encode("utf-8")).hexdigest()
        )
    
    # 历史末尾的 partial 回答（assistant）即续写前缀。
    return projection, source_ids, source_hashes


def prepare_context(
    *,
    prompt_bundle: PromptBundle,
    history: list[SourceMessage],
    current_input: str,
    profile: BudgetProfile,
    safety_margin_ratio: float = 0.1,
    warn_threshold_ratio: float = 0.75,
    strict: bool = False,
    approved_compact: CompactSnapshot | None = None,
    tool_schema_text: str | None = None,
    capability_note: str | None = None,
) -> ContextResult:
    """Vastar runtime component."""
    deduped = _dedup_by_source_id(history)
    messages, layers_executed, layers_not_executed = _apply_compact_layer(
        deduped, approved_compact)

    system_text = prompt_bundle.rendered_system()
    if capability_note:
        system_text = f"{system_text}\n\n{capability_note}"

    selection = select_and_gate(
        system_text=system_text, messages=messages, current_input=current_input,
        tool_schema_text=tool_schema_text, profile=profile,
        safety_margin_ratio=safety_margin_ratio, strict=strict,
        layers_executed=layers_executed, layers_not_executed=layers_not_executed,
        warn_threshold_ratio=warn_threshold_ratio)
    projection, source_ids, source_hashes = _project_messages(
        system_text, selection.messages, current_input)

    snapshot = ContextSnapshot(
        system_text=system_text,
        prompt_version=prompt_bundle.version,
        prompt_hash=prompt_bundle.content_hash,
        messages=projection,
        source_ids=source_ids,
        source_hashes=source_hashes,
        estimated_tokens=selection.measurement.estimated_tokens,
        budget=profile.input_budget,
        guarantee=selection.measurement.guarantee,
    )
    return ContextResult(
        snapshot=snapshot,
        measurement=selection.measurement,
        gate=selection.gate,
        layers_executed=selection.layers_executed,
        layers_not_executed=layers_not_executed,
        warnings=selection.warnings,
    )
