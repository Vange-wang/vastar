"""Vastar runtime component."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from core.skills.frontmatter import FrontmatterError, has_control_bytes, parse_frontmatter

log = logging.getLogger("vastar.skills")

REQUIRED_FIELDS = ("name", "description", "version", "triggers",
                   "permissions", "required_tools", "enabled")
ALLOWED_PERMISSIONS = ("A", "B")   # V4：权限先 A（只注入文字）+ B（只读工具）；C/D 待讨论
_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]*\Z")   # D1：命名一律 ASCII


@dataclass
class SkillMeta:
    """一个通过校验的技能（body 常驻内存但不进上下文——被选中才注入，D5）。"""

    name: str
    description: str
    version: str
    triggers: list[str]
    permissions: list[str]
    required_tools: list[str]
    enabled: bool
    needs_memory: bool
    source: str      # SKILL.md 路径（加载来源，随激活记录冻结到 Run）
    sha256: str      # 文件内容摘要（同上）
    body: str        # 正文（仅被选中时注入上下文）


@dataclass
class SkillFault:
    """一个显式不可用的技能（发现期判定，运行期不再碰它）。"""

    skill_dir: str
    reason: str


@dataclass
class Discovery:
    available: dict[str, SkillMeta] = field(default_factory=dict)
    unavailable: list[SkillFault] = field(default_factory=list)


def _validate(skill_dir: Path, text: str, tool_registry) -> SkillMeta | str:
    """单件校验：通过 → SkillMeta；失败 → 原因字符串（显式不可用）。"""
    if has_control_bytes(text):
        return "含控制字节"
    try:
        meta, body = parse_frontmatter(text)
    except FrontmatterError as exc:
        return f"frontmatter 非法：{exc}"
    missing = [k for k in REQUIRED_FIELDS if k not in meta]
    if missing:
        return f"缺必填字段：{'、'.join(missing)}"
    name = meta["name"]
    if not isinstance(name, str) or not _NAME_RE.match(name):
        return f"name 非法（须 ASCII 小写/数字/连字符）：{name!r}"
    for key in ("description", "version"):
        if not isinstance(meta[key], str) or not meta[key].strip():
            return f"字段 {key} 必须是非空字符串"
    if "needs_memory" in meta and not isinstance(meta["needs_memory"], bool):
        return "字段 needs_memory 必须是布尔（true/false）"
    for key in ("triggers", "permissions", "required_tools"):
        if not isinstance(meta[key], list) or not all(isinstance(x, str) for x in meta[key]):
            return f"字段 {key} 必须是字符串列表"
    if not isinstance(meta["enabled"], bool):
        return "字段 enabled 必须是布尔（true/false）"
    bad = [p for p in meta["permissions"] if p not in ALLOWED_PERMISSIONS]
    if bad:
        return f"permissions 超出本阶段 A+B 范围：{'、'.join(bad)}（禁升级父权限）"
    if (skill_dir / "scripts").is_dir():
        return "含 scripts/ 目录：执行脚本属权限 D，本批显式拒绝（待权限 D 开启）"
    for tool in meta["required_tools"]:
        spec = tool_registry.get(tool)
        if spec is None or not tool_registry.is_enabled(tool):
            return f"required_tools 未启用或不存在：{tool}"
        if spec.effect != "read":
            return f"required_tools 含非只读工具：{tool}（权限 B = 只读，禁升级）"
    return SkillMeta(
        name=name, description=str(meta["description"]), version=str(meta["version"]),
        triggers=[str(t) for t in meta["triggers"]],
        permissions=[str(p) for p in meta["permissions"]],
        required_tools=[str(t) for t in meta["required_tools"]],
        enabled=meta["enabled"], needs_memory=bool(meta.get("needs_memory", False)),
        source=str(skill_dir / "SKILL.md"),
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(), body=body)


def discover(skills_dir: Path, tool_registry) -> Discovery:
    """递归一层发现：skills/<name>/SKILL.md。目录不存在 = 无技能（正常态，非错误）。"""
    result = Discovery()
    duplicate_names: set[str] = set()
    if not skills_dir.is_dir():
        return result
    for child in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
        skill_md = child / "SKILL.md"
        if not skill_md.is_file():
            result.unavailable.append(SkillFault(child.name, "缺 SKILL.md"))
            continue
        try:
            text = skill_md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            result.unavailable.append(SkillFault(child.name, f"读取失败：{exc}"))
            continue
        outcome = _validate(child, text, tool_registry)
        if isinstance(outcome, str):
            result.unavailable.append(SkillFault(child.name, outcome))
        elif outcome.name in duplicate_names:
            result.unavailable.append(SkillFault(child.name, f"重复名：{outcome.name}"))
        elif outcome.name in result.available:
            # 重复名：两个都降级为不可用（禁静默二选一，D2）
            first = result.available.pop(outcome.name)
            duplicate_names.add(outcome.name)
            result.unavailable.append(SkillFault(Path(first.source).parent.name,
                                                 f"重复名：{outcome.name}"))
            result.unavailable.append(SkillFault(child.name, f"重复名：{outcome.name}"))
        else:
            result.available[outcome.name] = outcome
    for fault in result.unavailable:
        log.warning("skill 不可用：%s —— %s", fault.skill_dir, fault.reason)
    return result
