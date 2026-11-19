"""权限闸门（Spec §5 / R20 / R25）——执行层统一收口。

两级口径：
- 级别闸门（A/B/C/D）：R25 起权限 C 的「写文件」子项已开（写操作组 5 工具，本批实现）；
  C 其余项（联网检索）与 D（执行代码/命令）仍关闭、声明式拒绝（默认 closed 保护：
  当前无此类注册工具，后续接入须另立判定与裁定）。
- 操作判定（TASK-24 ①B / R25）：写操作组按「动作类别 × 目标位置类别」三值判定
  ALLOW / ASK / DENY（QD-114/QD-125：RECORD_ONLY 无适用场景、不设）；硬拒清单
  任何档任何位置 DENY 且优先（QD-113，任一目标命中 → 整批 DENY）；批量
  （>10 件 / >1MB / 递归目录）在项目内亦 ASK；删除一律 ASK（Vange 定案
  「所有删除都问」，QD-122）。

读取范围口径（R20）不变：整机只读 + 凭据类默认拒绝（黑名单在本模块统一持有，
工具实现内零判定——机械 grep 门）；MSYS 风格路径不要求支持，拒绝原因不得误报。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

OPEN_LEVELS = ("A", "B", "C")   # R25：C = 写文件子项已开（操作判定见 decide_operation）
_LEVEL_D_NOTE = ("权限级 D 未开启（执行代码/命令待 Vange 明确点名后按裁定开启；"
                 "C 的其余项——联网检索——同）")

_MSYS_STYLE_RE = re.compile(r"^/[A-Za-z](/|$)")
_CREDENTIAL_DIR_PARTS = {".ssh", ".hermes", ".aws"}
# 读取侧黑名单（R20；TASK-26 E 项修订：.env 移出——读放开、写仍硬拒，见 _ENV_NAME_PATTERNS）
_CREDENTIAL_NAME_PATTERNS = ("*.pem", "*.key", "*.p12", "*.pfx",
                             "id_rsa*", ".git-credentials", "*.kdbx", ".netrc",
                             ".netrc.*")
# .env 家族（R25 补充⑫ / ISSUE-HM-056）：读按位置档（根内 ALLOW / 根外 ASK），写一律 DENY
_ENV_NAME_PATTERNS = (".env", "*.env", ".env.*", "*.env.*")
_CREDENTIAL_DIR_REASON = ("路径 {path} 位于默认保护的凭据目录（{matched}/）；"
                          "如确需读取，请让 Hermes 出裁定后显式放开")
_CREDENTIAL_NAME_REASON = ("路径 {path} 属默认保护的凭据类（匹配 {matched}）；"
                           "如确需读取，请让 Hermes 出裁定后显式放开")
_MSYS_REASON = ("疑似 MSYS 风格路径（{raw}）：请改用 Windows 形式"
                "（请使用 {drive}: 盘符或相对路径）")

# ---- 操作判定（TASK-24 ①B / R25） ----------------------------------------------------

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
        raise PathRejected(_MSYS_REASON.format(raw=raw_path, drive=raw_path[1].upper()))
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


# ---- 硬拒清单与三值判定（TASK-24 ①B） -----------------------------------------------

def location_of(resolved: Path, project_root: Path) -> str:
    """目标位置类别：项目根内 in_root / 项目根外 out_root（根本身算根内）。

    HM-104（B12 交付项 D·defense-in-depth）：入口先归一化 `..`（resolve 不要求
    路径存在）——防未归一化路径词法逃逸（`proj/sub/../../outside/x` 实际根外，
    此前会被词法判 in_root）；根内 `sub/../x` 归一化后仍在根内（语义不变）。
    主路径 `resolve_read_path` 已 resolve，此处归一化幂等。
    """
    resolved = Path(resolved).resolve()
    root = Path(project_root).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return OUT_ROOT
    return IN_ROOT


def _system_dir_reason(resolved: Path) -> str | None:
    """系统目录（硬拒③）：Windows 顶层系统目录 + %LOCALAPPDATA% / %APPDATA% 及以下。

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
    """判定表（任务单 ①B，逐格有用例）：动作类别 × 位置类别 → ALLOW / ASK / DENY。

    - 删除（delete）：任何位置 ASK（Vange 定案「所有删除都问」，QD-122）；
    - 新建/编辑覆盖/移动：项目根内 ALLOW（备份照做）、项目外 ASK；
    - 批量（>BATCH_FILE_LIMIT 件 / >BATCH_BYTES_LIMIT / 递归目录）：项目内亦 ASK，
      且仅当"批量是触发 ASK 的原因"时会话记忆类别记为 batch（删除/项目外仍记原动作）；
    - DENY 只由硬拒清单产生（执行层 hard_deny_reason 先行一票否决，QD-113），
      本函数不再重复判定。
    """
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
