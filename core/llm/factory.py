"""provider 装配工厂（config/settings.json + 环境变量 → ProviderSpec）。

密钥只从环境变量读（.env 由 main.py 装载进 os.environ），绝不写入参数快照。
参数不跨供应商共用：params 只取各自 provider 块。
"""

from __future__ import annotations

import os
from pathlib import Path

from core.context import BudgetProfile
from core.errors import ErrorCategory, ErrorCode, VastarError
from core.llm.base import ProviderSpec
from core.llm.capabilities import configured_contract
from core.llm.providers.deepseek import DeepSeekProvider
from core.llm.providers.glm import GLMProvider
from core.llm.providers.openai_compat import OpenAICompatProvider

_PROVIDER_CLASSES = {
    "deepseek": DeepSeekProvider,
    "glm": GLMProvider,
}


# 兼容模型仅靠配置装配（llm.providers.<name>.protocol），不再改代码。
_PROTOCOL_CLASSES = {
    "deepseek": DeepSeekProvider,
    "glm": GLMProvider,
    "openai_compatible": OpenAICompatProvider,   # 通用实现，不做 provider 专属 amend_body 改写
}
_PROTOCOL_ALLOWED_TEXT = "deepseek / glm / openai_compatible"

_PARAM_KEYS = ("temperature", "top_p", "max_tokens", "thinking")


def load_env_file(path: str) -> int:
    """极简 .env 装载（KEY=VALUE，# 注释）；不覆盖已有环境变量。返回加载条数。"""
    count = 0
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and value and key not in os.environ:
            os.environ[key] = value
            count += 1
    return count


def _build_params(block: dict) -> dict:
    params = {k: block[k] for k in _PARAM_KEYS if k in block}
    params["max_tokens"] = int(block["max_output_tokens"])
    return params


def build_provider_spec(llm_cfg: dict, provider_name: str, context_cfg: dict,
                        model: str | None = None) -> ProviderSpec:
    """装配一个 provider 的运行规格；model 缺省用该 provider 的默认模型
    （主 provider 用 llm.model，其余用 models[0]）。/model 切换与降级共用本入口。"""
    registry = llm_cfg["providers"]
    if provider_name not in registry:
        raise VastarError(
            code=ErrorCode.PARAM_ERROR, category=ErrorCategory.PARAM,
            message=f"provider 不在注册表中：{provider_name}")
    block = registry[provider_name]
    api_key = os.environ.get(block["api_key_env"], "").strip()
    if not api_key:
        raise VastarError(
            code=ErrorCode.AUTH_ERROR, category=ErrorCategory.AUTH,
            message=f"环境变量 {block['api_key_env']} 未设置，无法装配 provider {provider_name}")
    base_url = os.environ.get(block["base_url_env"], "").strip() or block["base_url_default"]
    chosen_model = model or (
        llm_cfg["model"] if provider_name == llm_cfg["provider"] else block["models"][0])
    protocol = block.get("protocol")
    if protocol is not None:
        cls = _PROTOCOL_CLASSES.get(str(protocol))
        if cls is None:
            raise VastarError(
                code=ErrorCode.PARAM_ERROR, category=ErrorCategory.PARAM,
                message=(f"未知的 provider protocol：{protocol}"
                         f"（允许取值：{_PROTOCOL_ALLOWED_TEXT}）"))
    else:
        # 缺省行为不变：按既有名字映射（deepseek/glm 行为一字不改）
        cls = _PROVIDER_CLASSES.get(provider_name)
        if cls is None:
            raise VastarError(
                code=ErrorCode.PARAM_ERROR, category=ErrorCategory.PARAM,
                message=(f"未实现的 provider 类型：{provider_name}"
                         f"（第三方 OpenAI 兼容模型可在 llm.providers.{provider_name} "
                         f"加 \"protocol\": \"openai_compatible\"；允许取值："
                         f"{_PROTOCOL_ALLOWED_TEXT}）"))
    provider = cls(provider_name=provider_name, base_url=base_url, api_key=api_key)
    profile = BudgetProfile(
        provider=provider_name,
        model=chosen_model,
        product_input_limit=(None if context_cfg["max_input_tokens"] is None
                             else int(context_cfg["max_input_tokens"])),
        context_window=int(block["context_window"]),
        reserve_output_tokens=int(block["max_output_tokens"]),
    )
    return ProviderSpec(
        name=provider_name,
        provider=provider,
        model=chosen_model,
        params=_build_params(block),
        timeouts=dict(block.get("timeouts", {})),
        profile=profile,
        capabilities=configured_contract(chosen_model, profile, provider, block),
        pricing=dict(block.get("model_pricing", {}).get(chosen_model, block.get("pricing", {}))),
    )


def build_primary_and_fallback(
    llm_cfg: dict, context_cfg: dict,
) -> tuple[ProviderSpec, ProviderSpec | None]:
    primary_name = llm_cfg["provider"]
    primary = build_provider_spec(llm_cfg, primary_name, context_cfg)
    fallback: ProviderSpec | None = None
    fallback_name = llm_cfg.get("fallback_provider")
    if fallback_name and fallback_name in llm_cfg["providers"] and fallback_name != primary_name:
        fallback = build_provider_spec(llm_cfg, fallback_name, context_cfg)
    return primary, fallback
