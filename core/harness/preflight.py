"""最终发出文本的共同计量；分类只作分列，门总数来自完整消息与 schema。"""
import json
from dataclasses import replace

from core.errors import ErrorCategory, ErrorCode, VastarError
from core.token import check_gate, estimate_text_tokens, measure


def request_texts(messages, tools):
    texts = []
    for message in messages:
        content = message.get("content")
        if content is not None:
            texts.append(content if isinstance(content, str) else json.dumps(content, ensure_ascii=False))
        if message.get("tool_calls"):
            texts.append(json.dumps(message["tool_calls"], ensure_ascii=False))
        if message.get('reasoning_content'):
            texts.append(message['reasoning_content'])
    if tools:
        texts.append(json.dumps(tools, ensure_ascii=False))
    return texts


def context_decision(s, spec, request):
    reserve = int(request.params.get("max_tokens", spec.profile.reserve_output_tokens))
    profile = replace(spec.profile, reserve_output_tokens=reserve)
    declared_window = spec.capabilities["context_window"].value
    if declared_window is not None:
        profile.context_window = min(profile.context_window, int(declared_window))
    return check_gate(measure(request_texts(request.messages, request.tools)), profile,
                      safety_margin_ratio=float(s.request.token_cfg.get("safety_margin_ratio", 0.1)),
                      strict=s.request.strict_gate)


def estimate_components(s, request):
    parts = dict.fromkeys(("prompt", "tool_schema", "skill", "memory", "large_output_refs",
                           "current_input", "tool_results", "tool_arguments", "reasoning_history"), 0)
    for message in request.messages:
        content = message.get("content")
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False) if content is not None else ""
        count = estimate_text_tokens(text)
        if message.get("role") == "system":
            starts = [text.find(marker) for marker in ("可用技能清单", "已加载技能") if marker in text]
            skill = min(count, estimate_text_tokens(text[min(starts):])) if starts else 0
            parts["skill"] += skill
            parts["prompt"] += count - skill
        else:
            kind = ("skill" if text.startswith("已加载技能") else
                    "large_output_refs" if '"output_ref"' in text or "[大输出引用" in text else
                    "tool_results" if message.get("role") == "tool" else
                    "current_input" if message.get("role") == "user" and text == s.request.input_text else "memory")
            parts[kind] += count
        if message.get("tool_calls"):
            parts["tool_arguments"] += estimate_text_tokens(json.dumps(message["tool_calls"], ensure_ascii=False))
        if message.get('reasoning_content'):
            parts['reasoning_history'] += estimate_text_tokens(message['reasoning_content'])
    if request.tools:
        parts["tool_schema"] = estimate_text_tokens(json.dumps(request.tools, ensure_ascii=False))
    parts["output_reserve"] = int(request.params.get("max_tokens", 0))
    return {"input_tokens": sum(v for k, v in parts.items() if k != "output_reserve"),
            "components": parts, "guarantee": "estimated", "method": "heuristic_cjk_v1"}


def capability_check(call):
    spec, request = call.spec, call.request
    if (spec.name, spec.model) != (request.provider, request.model):
        raise VastarError(ErrorCode.PARAM_ERROR, ErrorCategory.PARAM,
                          "请求模型与能力/价格契约身份不符；拒绝错误归账")
    needed = ["stream"] + (["tools"] if request.tools else [])
    if not request.tools and any(m.get("tool_calls") or m.get("role") == "tool" for m in request.messages):
        needed.append("tool_history")
    if (request.params.get("thinking") or {}).get("type") == "enabled":
        needed.append("reasoning")
    missing = [name for name in needed if spec.capabilities[name].value is False]
    limit = spec.capabilities["output_constraints"].value.get("max_tokens")
    if missing or (limit is not None and request.params.get("max_tokens", 0) > limit):
        raise VastarError(ErrorCode.CAPABILITY_UNAVAILABLE, ErrorCategory.PARAM,
                          "配置能力/输出约束不允许本次请求", details={"capabilities": missing, "output_limit": limit})


def check_run_budget(s, estimate):
    if s.call_budget is None:
        cfg = s.request.token_cfg
        s.call_budget = {"max_calls": cfg.get("run_max_calls"),
                         "max_estimated_tokens": cfg.get("run_max_estimated_tokens"),
                         "estimated_tokens": 0}
    budget = s.call_budget
    required = estimate["input_tokens"] + estimate["components"]["output_reserve"]
    if ((budget["max_calls"] is not None and s.model_calls >= budget["max_calls"])
            or (budget["max_estimated_tokens"] is not None
                and budget["estimated_tokens"] + required > budget["max_estimated_tokens"])):
        raise VastarError(ErrorCode.INPUT_BUDGET_EXCEEDED, ErrorCategory.PARAM,
                          "共享 Run 调用/估算预算已耗尽；重试、降级、续写均不重置", details=dict(budget))
    return required
