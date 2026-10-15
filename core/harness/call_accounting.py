"""按 call 归账：usage 帧是累计快照，重复帧不加钱；未知字段绝不补零。"""
import math
from dataclasses import asdict


def _number(data, *names):
    for name in names:
        value = data.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
            return value
    return None


def record_usage(result, usage):
    values = {"input_tokens": _number(usage, "prompt_tokens", "input_tokens"),
              "output_tokens": _number(usage, "completion_tokens", "output_tokens"),
              "reasoning_tokens": _number(usage.get("completion_tokens_details") or {}, "reasoning_tokens")}
    changed = False
    for name, value in values.items():
        if value is not None and value > result.actual_usage.get(name, -1):
            result.actual_usage[name] = value
            changed = True
    result.usage_status = ("reported" if all(k in result.actual_usage for k in ("input_tokens", "output_tokens"))
                           else "partial" if result.actual_usage else "unknown")
    return changed


def calculate_cost(result):
    pricing = result.pricing
    rate_in = _number(pricing, "input_per_million")
    rate_out = _number(pricing, "output_per_million")
    if (result.usage_status == "reported" and rate_in is not None and rate_out is not None
            and pricing.get("currency") and pricing.get("version")):
        result.cost = (result.actual_usage["input_tokens"] * rate_in
                       + result.actual_usage["output_tokens"] * rate_out) / 1_000_000
        result.cost_status = "calculated_from_reported_usage"


def sync_run(s):
    s.result.budget_summary = dict(s.call_budget or {})
    calls = list(s.call_ledger.values())
    sent = [r for r in calls if r.started]
    known = [r for r in sent if r.actual_usage]
    s.model_calls = len(sent)
    s.result.model_calls = s.model_calls
    s.result.usage_reports = len(known)
    totals = {}
    if known:
        for name in ("input_tokens", "output_tokens", "reasoning_tokens"):
            totals[name] = (sum(r.actual_usage[name] for r in sent)
                            if all(name in r.actual_usage for r in sent) else "unknown")
    s.usage_totals = s.result.usage_totals = totals
    s.result.call_records = [asdict(r) for r in calls]
    s.result.usage_status = ("reported" if sent and all(r.usage_status == "reported" for r in sent)
                             else "partial" if known else "unknown")
    currencies = {r.pricing.get("currency") for r in sent}
    s.result.cost_summary = {"status": "unknown", "amount": None, "currency": None,
                            "known_costs_by_currency": {}}
    for r in sent:
        if r.cost is not None:
            currency = r.pricing["currency"]
            known_cost = s.result.cost_summary["known_costs_by_currency"]
            known_cost[currency] = known_cost.get(currency, 0) + r.cost
    if sent and len(currencies) == 1 and all(r.cost is not None for r in sent):
        s.result.cost_summary.update(status="calculated_from_reported_usage",
                                     amount=sum(r.cost for r in sent), currency=next(iter(currencies)))
