"""Vastar runtime component."""

from __future__ import annotations

import logging
import os
from pathlib import Path

# (注册表值名, 展示标签, %USERPROFILE% 兜底标准名)
KNOWN_ENTRIES = (
    ("Desktop", "桌面", "Desktop"),
    ("Personal", "文档", "Documents"),
    ("{374DE290-123F-4565-9164-39C4925E467B}", "下载", "Downloads"),
    ("My Pictures", "图片", "Pictures"),
    ("My Music", "音乐", "Music"),
    ("My Video", "视频", "Videos"),
)
_REGISTRY_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"

_WHITELIST_NOTE = ("【环境事实】系统真实位置（仅事实告知，不构成写入白名单——"
                   "写入这些位置仍按权限判定，项目外仍需确认）：")


def _registry_shell_folders() -> dict[str, str] | None:
    """读 User Shell Folders 全部值；注册表打不开/读失败 → None（整段不注入）。"""
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REGISTRY_SUBKEY) as key:
            count = winreg.QueryInfoKey(key)[1]
            return {winreg.EnumValue(key, i)[0]: winreg.EnumValue(key, i)[1]
                    for i in range(count)}
    except OSError:
        return None


def _expand(raw: str) -> str | None:
    """展开 %USERPROFILE% 等环境变量；仍有未展开变量或结果为空 → None（该条不注入）。"""
    expanded = os.path.expandvars(raw)
    if not expanded or "%" in expanded:
        return None
    return expanded


def collect_known_paths(collect_registry=_registry_shell_folders) -> dict[str, str] | None:
    """返回 {标签: 路径}；注册表不可读 → None（调用方整段不注入，禁默认错路径）。"""
    values = collect_registry()
    if values is None:
        return None
    profile = os.environ.get("USERPROFILE", "")
    facts: dict[str, str] = {}
    for reg_name, label, std_name in KNOWN_ENTRIES:
        raw = values.get(reg_name)
        if raw:
            expanded = _expand(str(raw))
            if expanded is not None:
                facts[label] = expanded
                continue
        if profile:   # 单条缺失 → 标准名兜底（注册表可读 = 机器配置可知）
            facts[label] = str(Path(profile) / std_name)
        # 再读不到 → 该条不注入
    return facts


def format_env_facts(env_cfg: dict | None,
                     collect=collect_known_paths) -> str:
    """渲染「环境事实」段；开关关 / 读表失败 / 非 Windows → 空串（不注入）。"""
    if not (env_cfg or {}).get("inject_known_paths", True):
        return ""
    facts = collect()
    if facts is None:
        if os.name == "nt":
            logging.getLogger("vastar.env").warning(
                "已知文件夹注册表读取失败——本次不注入环境事实段（禁默认错路径）")
        return ""
    if not facts:
        return ""
    lines = [_WHITELIST_NOTE]
    lines.extend(f"- {label}：{path}" for label, path in facts.items())
    return "\n".join(lines)
