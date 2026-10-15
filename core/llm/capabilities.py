"""供应商能力是带出处的配置声明，不是模型支持上限或自动认证。"""
from dataclasses import asdict, dataclass
from datetime import datetime

CAPABILITIES = ("tools", "stream", "reasoning", "tool_history", "context_window", "output_constraints")
VERIFICATION_STATES = ("configured", "live_verified", "tool_verified")


@dataclass(frozen=True)
class CapabilityClaim:
    value: object
    status: str = "configured"
    verified_at: str | None = None
    model_version: str | None = None
    evidence: str | None = None

    def __post_init__(self):
        if self.status not in VERIFICATION_STATES:
            raise ValueError(f"未知能力验证状态：{self.status}")
        if self.status != "configured" and not all((self.verified_at, self.model_version, self.evidence)):
            raise ValueError("已验证能力必须记录验证时间、模型版本和证据；mock 不升级真实能力状态")
        if self.verified_at is not None and datetime.fromisoformat(self.verified_at).tzinfo is None:
            raise ValueError("能力验证时间必须包含时区")


def configured_contract(model, profile, provider, block=None):
    block = block or {}
    defaults = {"tools": True, "stream": block.get("stream", True), "reasoning": None,
                "tool_history": getattr(provider, "supports_tool_messages_without_tools", True),
                "context_window": profile.context_window,
                "output_constraints": {"requested_tokens": profile.reserve_output_tokens,
                                       "max_tokens": block.get("max_output_tokens_limit")}}
    declared = {**block.get("capabilities", {}),
                **block.get("model_capabilities", {}).get(model, {})}
    return {name: CapabilityClaim(**declared[name]) if name in declared else
            CapabilityClaim(value=value, model_version=model)
            for name, value in defaults.items()}


def contract_record(spec):
    return {"provider": spec.name, "model": spec.model,
            "capabilities": {key: asdict(value) for key, value in spec.capabilities.items()},
            "pricing": dict(spec.pricing), "pricing_status": "configured" if spec.pricing else "unknown"}
