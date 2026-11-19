"""Validate configured prices before a budgeted task makes a model call."""
import math
from datetime import date
from urllib.parse import urlparse


def require_pricing(pricing, provider, model, *, official=False):
    prefix = f"模型 {provider}/{model} 定价无效：llm.providers.{provider}.model_pricing.{model}"

    def fail(field):
        raise ValueError(f"{prefix}.{field} 缺失或无效；请配置有效 CNY 官方价目后再执行任务")

    if not isinstance(pricing, dict):
        fail("pricing")
    if pricing.get("currency") != "CNY":
        fail("currency")
    if not isinstance(pricing.get("version"), str) or not pricing["version"].strip():
        fail("version")
    def rate(block, key, path):
        value = block.get(key) if isinstance(block, dict) else None
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            fail(path)
        return value
    keys = ("input_per_million", "output_per_million")
    for key in keys:
        rate(pricing, key, key)
    if not official:
        return
    source = pricing.get("source")
    if not isinstance(source, str) or urlparse(source).scheme != "https" or not urlparse(source).netloc:
        fail("source")
    try:
        date.fromisoformat(pricing["checked_at"])
    except (KeyError, TypeError, ValueError):
        fail("checked_at")
    for period in ("peak", "off_peak"):
        for key in (*keys, "cache_hit_per_million"):
            rate(pricing.get(period), key, f"{period}.{key}")
    for key in keys:
        if pricing[key] != pricing["peak"][key]:
            fail(f"{key}（须等于 peak.{key}）")


def validate_task_pricing(raw):
    if not raw.get("orchestration", {}).get("enabled", False):
        return
    for name, entry in raw["llm"]["providers"].items():
        for model, price in entry.get("model_pricing", {}).items():
            require_pricing(price, name, model, official=True)


def require_task_model(coord):
    tools = getattr(coord, "task_tools_context", None)
    config = getattr(tools, "team_entry_config", None)
    if config is not None and config.enabled:
        spec = coord._primary
        require_pricing(spec.pricing, spec.name, spec.model)
