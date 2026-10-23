"""Vastar runtime component."""
from __future__ import annotations

import json
import re
from pathlib import Path

from ui.theme import BRIGHT_BLUE, BRIGHT_YELLOW, RESET, gradient_rule

DEFAULT_USER_NAME = "Star-User"
USER_NAME_CAP = 24


def welcome_text(width: int | None = None) -> str:
    """紧凑欢迎块：渐变线＋品牌行＋入口提示（无任何预检/模型/诊断长文）。"""
    top = gradient_rule(width)
    bottom = gradient_rule(width)
    brand = (f"{BRIGHT_YELLOW}VASTAR{RESET} "
             f"{BRIGHT_BLUE}· 个人专属 Agent{RESET}")
    hint = f"{BRIGHT_BLUE}/help 命令 · /status 状态 · Ctrl+C 取消{RESET}"
    return f"{top}\n{brand}\n{hint}\n{bottom}\n"


def sanitize_user_name(text: str) -> str:
    """称呼清洗：去控制字符/换行、去首尾空白、上限 24 字符；空 → 默认名。"""
    cleaned = "".join(ch for ch in str(text) if ch.isprintable()).strip()
    return cleaned[:USER_NAME_CAP] or DEFAULT_USER_NAME


def write_user_name(config_path: str | Path, name: str) -> str:
    """把称呼写入 config `ui.user_name`（长期沿用）；返回实际写入的名字。

    文本级操作：已有键 → 只替换该键值；无键 → 在 `"ui": {` 后插入一行。
    找不到 ui 对象则显式报错（不静默、不重排整份配置）。
    """
    path = Path(config_path)
    name = sanitize_user_name(name)
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)   # 先验证 JSON 合法，避免写坏配置
    if not isinstance(data.get("ui"), dict):
        raise ValueError(f"{path} 缺少 ui 对象，无法写入 user_name")
    encoded = json.dumps(name, ensure_ascii=False)
    pattern = re.compile(r'("user_name"\s*:\s*)"(?:[^"\\]|\\.)*"')
    if '"user_name"' in raw:
        updated, count = pattern.subn(lambda m: m.group(1) + encoded, raw, count=1)
        if count != 1:
            raise ValueError(f"{path} 的 ui.user_name 文本定位失败")
    else:
        anchor = re.search(r'"ui"\s*:\s*\{\s*\n([ \t]*)', raw)
        if anchor is None:
            raise ValueError(f"{path} 的 ui 对象不是多行 JSON，拒绝重排写入")
        indent = anchor.group(1)
        insert_at = anchor.end()
        updated = raw[:insert_at] + f'"user_name": {encoded},\n{indent}' + raw[insert_at:]
    json.loads(updated)      # 写前再验证一次（不合法则绝不落盘）
    path.write_text(updated, encoding="utf-8")
    return name
