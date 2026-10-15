"""Vastar runtime component."""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class PageRequest:
    """分页请求（冻结接口）：cursor=None 表示首页；page_size 上限由实现批（B16）定。"""

    cursor: str | None = None
    page_size: int = 200


@dataclass(frozen=True)
class PageResult:
    """分页结果（冻结接口）：next_cursor=None 表示末页。"""

    items: tuple
    next_cursor: str | None = None
    has_more: bool = False


def encode_cursor(payload: dict) -> str:
    """游标编码：JSON（sort_keys）→ base64url。载荷须含 owner/scope/hash（B16·B.4 校验项）。"""
    return base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str) -> dict:
    """游标解码：非法形态抛 ValueError（调用方转显式失效，禁静默当首页）。"""
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
    except Exception as exc:
        raise ValueError(f"游标非法：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("游标非法：不是 JSON 对象")
    return data
