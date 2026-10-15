"""Attempt 是编排尝试；Call 是一次供应商请求，二者不共用计数。"""
import hashlib
import json
import uuid
from dataclasses import dataclass, field

from core.llm.base import LLMRequest, ProviderSpec

ENTRIES = ("initial", "retry", "follow_up", "final_answer", "continuation")


@dataclass
class ModelCallRequest:
    request: LLMRequest
    spec: ProviderSpec
    attempt_id: str
    entry: str
    call_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    context_ref: str = ""
    budget_policy: object = None  # 工具预算拒绝的既有事件/提示策略；仅内部注入。

    def __post_init__(self):
        if self.entry not in ENTRIES:
            raise ValueError(f"未知模型入口：{self.entry}")
        if not self.context_ref:
            self.context_ref = "context:" + self.fingerprint()

    def fingerprint(self):
        wire = [self.spec.name, self.spec.model, self.request.params,
                self.request.messages, self.request.tools]
        return hashlib.sha256(json.dumps(wire, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@dataclass
class ModelCallResult:
    call_id: str
    attempt_id: str
    context_ref: str
    provider: str
    model: str
    entry: str
    fingerprint: str
    status: str = "preflight"
    started: bool = False
    usage_status: str = "unknown"
    actual_usage: dict = field(default_factory=dict)
    estimated_usage: dict = field(default_factory=dict)
    cost: float | None = None
    cost_status: str = "unknown"
    pricing: dict = field(default_factory=dict)
    error: dict | None = None
    entries_seen: list = field(default_factory=list)


def request_for(s, spec, request, attempt_id):
    phase = request.meta.get("phase")
    entry = ("continuation" if request.meta.get("continuation") else
             "follow_up" if phase == "final_answer" else
             "final_answer" if phase == "degraded_answer" else
             "retry" if len(s.result.attempt_ids) > 1 else "initial")
    return ModelCallRequest(request, spec, attempt_id, entry,
                            call_id=request.meta.get("call_id") or uuid.uuid4().hex,
                            budget_policy=request.meta.get("_budget_policy"))
