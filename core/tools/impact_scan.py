"""目录影响面扫描（TASK-45/B10·B 交付项）：移动/删除目录前的显式影响对象扫描。

覆盖四类显式检查（任务单 ②B）：
- 后代敏感路径：命中执行层凭据黑名单 / .env 家族 / 硬拒清单的后代逐条列出；
- 链接/junction：符号链接与 Windows reparse point（junction）逐条列出
  （遍历不跟随链接，链接本体计入受影响集合，链接目标内容不探测）；
- 大小写/规范路径：normcase 归一后的同目录大小写异写冲突检测；
- TOCTOU（检查后替换）：plan 时取指纹、授权后执行前复核，不一致即显式拒绝。

口径红线（任务单 ②B2 / ③2）：本模块只产出「已检出」事实清单；
「未检出」仅指本次扫描范围内未命中，不等于"不存在风险"；未覆盖的维度一律报「未测」。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from core.tools import permissions

_REPARSE_POINT = 0x00000400   # FILE_ATTRIBUTE_REPARSE_POINT（Windows junction 等）


def is_link(path: Path) -> bool:
    """符号链接或 Windows reparse point（junction 等）。探测失败按「未测」处理（返回 False）。"""
    try:
        if path.is_symlink():
            return True
        if os.name == "nt":
            return bool(path.stat(follow_symlinks=False).st_file_attributes & _REPARSE_POINT)
    except OSError:
        return False
    return False


def norm_key(path: Path) -> str:
    """规范路径键（Windows 大小写不敏感归一，normcase）。"""
    return os.path.normcase(str(path))


@dataclass
class ImpactReport:
    """一次影响面扫描的事实清单（只报「已检出」；未命中 = 本次范围内未检出）。"""

    entries: int = 0
    total_bytes: int = 0
    links: list[str] = field(default_factory=list)          # 已检出：链接/junction 后代
    sensitive: list[str] = field(default_factory=list)      # 已检出：命中硬拒/凭据黑名单的后代
    case_conflicts: list[str] = field(default_factory=list)  # 已检出：同目录大小写异写冲突
    unverified: list[str] = field(default_factory=list)     # 未测：stat 失败项（红线 :10-12）

    def summary(self) -> str:
        parts = [f"{self.entries} 项 / {self.total_bytes} 字节"]
        if self.links:
            parts.append(f"含链接/junction {len(self.links)} 个（{'; '.join(self.links[:3])}）")
        if self.sensitive:
            parts.append(f"含敏感路径 {len(self.sensitive)} 个")
        if self.case_conflicts:
            parts.append(f"含大小写异写冲突 {len(self.case_conflicts)} 处")
        if self.unverified:
            parts.append(f"未测 {len(self.unverified)} 项（stat 失败，字节口径不完整："
                         f"{'; '.join(self.unverified[:3])}）")
        return "，".join(parts)


def scan_tree(root: Path, project_root: Path) -> ImpactReport:
    """扫描目录后代：敏感路径、链接/junction、大小写冲突。遍历不跟随链接。

    敏感路径判定复用执行层既有清单（hard_deny_reason / check_credential_path）——
    不新增口径（R20/R25 有效口径不动，任务单 ③3）。
    """
    report = ImpactReport()
    if not root.is_dir():
        return report
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        seen: dict[str, str] = {}
        for name in [*dirnames, *filenames]:
            p = Path(dirpath) / name
            report.entries += 1
            if is_link(p):
                report.links.append(str(p))
                continue                      # 链接本体入集；目标内容不探测（未测 ≠ 安全）
            key = norm_key(p)
            if key in seen and seen[key] != name:
                report.case_conflicts.append(f"{seen[key]} ↔ {name}")
            seen.setdefault(key, name)
            if permissions.hard_deny_reason(p, project_root) is not None \
                    or permissions.check_credential_path(p) is not None:
                report.sensitive.append(str(p))
            try:
                if p.is_file():
                    report.total_bytes += p.stat().st_size
            except OSError as exc:
                # §三-10：红线 :10-12「未覆盖的维度一律报未测」——stat 失败显式入
                # 未测清单（字节口径不完整可观测），不得 except: pass 静默吞没。
                report.unverified.append(f"{p} [{type(exc).__name__}]")
    return report


def case_conflict_for(target: Path) -> str | None:
    """目标不存在但同目录存在大小写异写的同名项（Windows 上操作会实际命中该项）。"""
    if target.exists():
        return None
    try:
        for sibling in target.parent.iterdir():
            if norm_key(sibling) == norm_key(target) and sibling.name != target.name:
                return str(sibling)
    except OSError:
        return None
    return None


def fingerprint(paths) -> dict[str, tuple]:
    """保存成员集合和文件身份；授权等待后新增后代也使旧计划失效。

    不跟随目录链接；无法读取的维度记 unverified，复核不得视为相等放行。
    这是执行前快照复核，不承诺与随后 OS 操作组成原子事务。
    """
    fp: dict[str, tuple] = {}
    for p in paths:
        p = Path(p)
        try:
            st = p.lstat()
            identity = (st.st_dev, st.st_ino, st.st_mode)
            if is_link(p):
                fp[str(p)] = ("link", identity, os.readlink(p))
            elif p.is_dir():
                fp[str(p)] = ("directory", identity,
                              tuple(sorted(child.name for child in p.iterdir())))
            else:
                fp[str(p)] = ("file", identity, st.st_size, st.st_mtime_ns)
        except FileNotFoundError:
            fp[str(p)] = ("absent",)
        except OSError as exc:
            fp[str(p)] = ("unverified", type(exc).__name__)
    return fp


def recheck(before: dict[str, tuple]) -> list[str]:
    """旧对象发生变化或无法核实均返回差异；新增成员由目录快照检出。"""
    after = fingerprint(before)
    return [key for key, snap in before.items()
            if snap[0] == "unverified" or after[key][0] == "unverified" or after[key] != snap]
