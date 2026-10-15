"""Vastar runtime component."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

OPEN_LEVELS = ("A", "B", "C")   # R25：C = 写文件子项已开（操作判定见 decide_operation）
_LEVEL_D_NOTE = ("权限级 D 未开启（执行代码/命令待 user 明确点名后按裁定开启；"
                 "C 的其余项——联网检索——同）")

_MSYS_STYLE_RE = re.compile(r"^/[A-Za-z](/|$)")
_CREDENTIAL_DIR_PARTS = {".ssh", ".hermes", ".aws"}

_CREDENTIAL_NAME_PATTERNS = ("*.pem", "*.key", "*.p12", "*.pfx",
                             "id_rsa*", ".git-credentials", "*.kdbx", ".netrc",
                             ".netrc.*")

_ENV_NAME_PATTERNS = (".env", "*.env", ".env.*", "*.env.*")
_CREDENTIAL_DIR_REASON = ("路径 {path} 位于默认保护的凭据目录（{matched}/）；"
                          "如确需读取，请让 Hermes 出裁定后显式放开")
_CREDENTIAL_NAME_REASON = ("路径 {path} 属默认保护的凭据类（匹配 {matched}）；"
                           "如确需读取，请让 Hermes 出裁定后显式放开")
_MSYS_REASON = ("疑似 MSYS 风格路径（{raw}）：请改用 Windows 形式"
                "（如 D:\\… 或相对路径）")



PERMISSION_ASK = "ask"
PERMISSION_AUTO = "auto"
VERDICT_ALLOW = "ALLOW"
VERDICT_ASK = "ASK"
VERDICT_DENY = "DENY"

BATCH_FILE_LIMIT = 10             # 单次 >10 个文件 → 批量
BATCH_BYTES_LIMIT = 1024 * 1024   # 单次 >1MB → 批量

IN_ROOT = "in_root"
OUT_ROOT = "out_root"

_GIT_DIRNAME = ".git"
_PROJECT_STATE_TOPS = ("config", "logs", "memory")
_FALLBACK_TOPS = ("_backups", "_trash")
_SYSTEM_TOPS = ("windows", "program files", "program files (x86)", "programdata")
_APPDATA_PART = "appdata"

_GIT_REASON = "硬拒清单② .git/ 内部（防自毁工作区，任何档任何位置拒绝）"
_SYSTEM_REASON = "硬拒清单③ 系统目录"
_FALLBACK_REASON = ("硬拒清单④ 兜底设施（防「覆盖前备份/删除可逆」被绕过，"
                    "任何档任何位置拒绝）")
_STATE_REASON = ("硬拒清单④ 项目自身状态（config/logs/memory——V1 配置基线与"
                 " V3 审计证据链，任何档任何位置拒绝）")


class PathRejected(Exception):
    """路径被安全策略拒绝（凭据类 / MSYS 形态）——执行层转为显式 rejected。"""


@dataclass
class GateDecision:
    allowed: bool
    reason: str = ""


@dataclass
class OperationDecision:
    """一次写操作的三值判定结果（执行层消费；audit 与询问会话记忆键共用）。"""

    verdict: str                # ALLOW / ASK / DENY
    reason: str = ""
    action_category: str = ""   # 会话记忆键·动作半：create/edit/move/delete/batch
    location_category: str = ""  # 会话记忆键·位置半：in_root/out_root
    batch: bool = False


def check_level(level: str) -> GateDecision:
    """级别闸门：A/B/C 放行（C=R25 写文件子项，操作判定在 decide_operation）；D 拒绝。"""
    if level in OPEN_LEVELS:
        return GateDecision(allowed=True)
    return GateDecision(allowed=False, reason=_LEVEL_D_NOTE)


def resolve_read_path(project_root: Path, raw_path: str) -> Path:
    """路径解析 + 安全校验（全部工具共用）：通过即返回实路径。

    相对路径按项目根解析；绝对路径原样（整机范围，R20）；符号链接按 resolve() 实解析。
    MSYS 风格与凭据类路径 → PathRejected（显式，禁静默）。
    """
    if _MSYS_STYLE_RE.match(raw_path.replace("\\", "/")):
        raise PathRejected(_MSYS_REASON.format(raw=raw_path))
    root = project_root.resolve()
    candidate = Path(raw_path)
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    reason = check_credential_path(resolved)
    if reason is not None:
        raise PathRejected(reason)
    return resolved


def check_credential_path(resolved: Path) -> str | None:
    """读取侧凭据类黑名单（R20；.env 已移出——见 is_env_path）：命中返回原因，未命中 None。"""
    for part in resolved.parts:
        if part.lower() in _CREDENTIAL_DIR_PARTS:
            return _CREDENTIAL_DIR_REASON.format(path=resolved, matched=part)
    name = resolved.name.lower()
    for pattern in _CREDENTIAL_NAME_PATTERNS:
        if fnmatch(name, pattern):
            return _CREDENTIAL_NAME_REASON.format(path=resolved, matched=pattern)
    return None


def is_env_path(resolved: Path) -> bool:
    """.env 家族判定（R25 补充⑫）：读按位置档、写硬拒；根外发现不绕过 ASK。"""
    name = resolved.name.lower()
    return any(fnmatch(name, pattern) for pattern in _ENV_NAME_PATTERNS)


_ENV_WRITE_REASON = ("硬拒清单① 凭据类 .env（写操作——防自我提权：改 .env 即改自己的"
                     " key/base_url/fallback；读已按 R25 补充⑫放开，任何档任何位置拒绝写）")




def location_of(resolved: Path, project_root: Path) -> str:
    """Vastar runtime component."""
    resolved = Path(resolved).resolve()
    root = Path(project_root).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return OUT_ROOT
    return IN_ROOT


def _system_dir_reason(resolved: Path) -> str | None:
    """系统目录（硬拒③）：Windows 顶层系统目录 + C:\\Users\\<u>\\AppData 及以下。

    豁免：AppData\\Local\\Temp（OS 标准临时区——Vastar 原子写 mkstemp 亦落此，
    无保护价值；豁免口径随自测报告送审）。非绝对路径不判（交由其他类别）。
    """
    if not resolved.is_absolute():
        return None
    parts = [p.lower() for p in resolved.parts]
    if os.name == "nt" and len(parts) >= 2 and parts[1] in _SYSTEM_TOPS:
        return f"{_SYSTEM_REASON}（{parts[1]}）：{resolved}"
    if _APPDATA_PART in parts:
        idx = parts.index(_APPDATA_PART)
        if len(parts) > idx + 2 and parts[idx + 1] == "local" and parts[idx + 2] == "temp":
            return None
        return f"{_SYSTEM_REASON}（AppData）：{resolved}"
    return None


def hard_deny_reason(resolved: Path, project_root: Path,
                     protected_paths: tuple[Path, ...] = ()) -> str | None:
    """硬拒清单（任何档任何位置 → DENY）。命中返回原因，未命中返回 None。

    ① 凭据类（与读取黑名单同源；.env 家族对写单独硬拒——读已放开，见 is_env_path）；
    ② .git/ 内部；③ 系统目录；
    ④ 项目自身状态（config/logs/memory）与兜底设施（memory/_backups、memory/_trash，
    显式列出——置于 memory/ 之下自动受④覆盖，仍单列原因以便审计可读）。
    """
    resolved = resolved.resolve()
    for protected in protected_paths:
        protected = protected.resolve()
        if resolved.is_relative_to(protected) or protected.is_relative_to(resolved):
            return f"硬拒运行状态/配置路径及其祖先：{resolved}（保护 {protected}）"
    if is_env_path(resolved):
        return f"{_ENV_WRITE_REASON}：{resolved}"
    credential = check_credential_path(resolved)
    if credential is not None:
        return f"硬拒清单① 凭据类（任何档任何位置拒绝）：{credential}"
    if _GIT_DIRNAME in resolved.parts:
        return f"{_GIT_REASON}：{resolved}"
    system = _system_dir_reason(resolved)
    if system is not None:
        return system
    if protected_paths:
        for parent in (resolved, *resolved.parents):
            if (parent / "owner.json").is_file() and (parent / "migration-result.json").is_file():
                return f"硬拒项目运行数据：{resolved}"
        if ".vastar" in resolved.parts:
            return f"硬拒项目身份/配置：{resolved}"
        return None  # 新模式保护实际状态位置，外部项目的同名 memory/logs 不冒充程序状态。
    try:
        rel = resolved.relative_to(project_root)
    except ValueError:
        return None
    if rel.parts:
        top = rel.parts[0].lower()
        second = rel.parts[1].lower() if len(rel.parts) >= 2 else ""
        if top == "memory" and second in _FALLBACK_TOPS:
            return f"{_FALLBACK_REASON}：{resolved}"
        if top in _PROJECT_STATE_TOPS:
            return f"{_STATE_REASON}：{resolved}"
    return None


def decide_operation(*, action: str, targets: list[Path], project_root: Path,
                     count: int = 1, total_bytes: int = 0,
                     recursive: bool = False) -> OperationDecision:
    """Vastar runtime component."""
    out_root = any(location_of(t, project_root) == OUT_ROOT for t in targets)
    location = OUT_ROOT if out_root else IN_ROOT
    batch = count > BATCH_FILE_LIMIT or total_bytes > BATCH_BYTES_LIMIT or recursive
    if action == "delete":
        return OperationDecision(VERDICT_ASK, "删除一律询问（QD-122）", action, location, batch)
    if location == OUT_ROOT:
        return OperationDecision(VERDICT_ASK, "项目外写操作需确认（R25）", action, location, batch)
    if batch:
        return OperationDecision(VERDICT_ASK, f"批量操作需确认（{count} 项 / {total_bytes} 字节）",
                                 "batch", location, batch)
    return OperationDecision(VERDICT_ALLOW, "项目根内常规写操作（备份照做）", action, location, batch)
