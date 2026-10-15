"""SKILL.md 极简 YAML frontmatter 解析（skills-decisions v1.2 D1：项目内实现，零第三方依赖）。

支持形态（刻意最小；日后需要复杂结构再评估，不得暗改已定格式）：
    ---
    key: value          # 标量（true/false → bool，其余为字符串；可加单/双引号）
    key:                # 空值 + 后随 "- item" 行 → 字符串列表
      - item
    ---
    正文（markdown）……
任何形态问题都抛 FrontmatterError，由发现层转为"显式不可用"（D2 禁静默）。
"""

from __future__ import annotations


class FrontmatterError(ValueError):
    """frontmatter 缺失或形态非法。"""


def _strip_scalar(raw: str):
    val = raw.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
        val = val[1:-1]
    low = val.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    return val


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 SKILL.md → (元数据 dict, 正文)。形态非法抛 FrontmatterError。"""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise FrontmatterError("缺少 --- 起始的 frontmatter 头")
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        raise FrontmatterError("frontmatter 缺少 --- 收尾行")
    meta: dict = {}
    current_key: str | None = None
    for lineno, raw in enumerate(lines[1:end], start=2):
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("- "):
            if current_key is None or not isinstance(meta.get(current_key), list):
                raise FrontmatterError(f"第 {lineno} 行：列表项没有所属列表键")
            meta[current_key].append(str(_strip_scalar(stripped[2:])))
            continue
        if ":" not in stripped:
            raise FrontmatterError(f"第 {lineno} 行：不是 key: value 形态")
        key, _, value = stripped.partition(":")
        key = key.strip()
        if not key:
            raise FrontmatterError(f"第 {lineno} 行：空键")
        if key in meta:
            raise FrontmatterError(f"第 {lineno} 行：重复字段 {key}")
        value = value.strip()
        meta[key] = [] if not value else _strip_scalar(value)
        current_key = key
    return meta, "\n".join(lines[end + 1:]).strip()


def has_control_bytes(text: str) -> bool:
    """控制字节检测（\\t\\n\\r 之外；含 0x7f）：D2 校验项，命中即显式不可用。"""
    return any((ord(ch) < 0x20 and ch not in "\t\n\r") or ord(ch) == 0x7F for ch in text)
