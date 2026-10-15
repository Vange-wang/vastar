"""工具调用审计（Spec §6）：事件 payload 构造 + 参数摘要（敏感值不落库）+ 来源区分。

来源 origin_source ∈ {user, model, skill}（skill 为 skill 阶段预留）——
放在事件 payload 的 source 字段（事件信封的 origin 字段沿用会话口径 user/test/system）。
"""

from __future__ import annotations

import json

ARGS_SUMMARY_MAX = 200
_RESULT_PREVIEW_CHARS = 80
_REDACT_STR_LEN = 64     # 超长字符串值脱敏阈值（write_file 的 content 等大对象绝不落审计）


def _redact(value):
    """递归脱敏：超长字符串 → 长度占位（保留参数结构，内容不落库）。"""
    from memory.policy import sensitive
    if isinstance(value, str) and sensitive(value):
        return '<protected>'
    if isinstance(value, str) and len(value) > _REDACT_STR_LEN:
        return f"<str:{len(value)}字符>"
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def args_summary(args: dict) -> str:
    """参数摘要：长字符串值脱敏后 JSON 序列化并截断（敏感值不落库）。"""
    try:
        text = json.dumps(_redact(args), ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        text = repr(args)
    if len(text) > ARGS_SUMMARY_MAX:
        return text[:ARGS_SUMMARY_MAX] + "…（截断）"
    return text


def result_fields(result: str) -> dict:
    """结果审计字段：只记字节数与短预览（内容不落库）。"""
    data = result.encode("utf-8")
    return {"result_bytes": len(data), "preview": result[:_RESULT_PREVIEW_CHARS]}
