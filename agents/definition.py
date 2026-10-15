"""Vastar runtime component."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AgentDefinition:
    name: str
    version: str
    locale: str
    owner: str
    max_model_steps: int
    max_attempts_per_step: int
    recent_raw_turns: int
    session_mode: str                     # queue / interrupt
    config_path: Path
    config_version_hash: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def llm(self) -> dict:
        return self.raw["llm"]

    @property
    def token_cfg(self) -> dict:
        return self.raw["token"]

    @property
    def context_cfg(self) -> dict:
        return self.raw["context"]

    @property
    def prompt_cfg(self) -> dict:
        return self.raw["prompt"]

    @property
    def memory_cfg(self) -> dict:
        return self.raw["memory"]

    @property
    def orchestration_cfg(self):
        from core.orchestration.config import load_orchestration
        return load_orchestration(self.raw)


def _need(block: dict, key: str, where: str) -> None:
    """Vastar runtime component."""
    if key not in block:
        raise ValueError(f"{where} 缺少必填键：{key}")


def load_agent(config_path: str | Path, *, raw: dict | None = None) -> AgentDefinition:
    """装载 Agent 定义（含三段式 loop 参数与配置基线哈希）。"""
    path = Path(config_path)
    raw = json.loads(path.read_text(encoding="utf-8")) if raw is None else raw
    for section in ("agent", "runtime", "llm", "context", "token", "prompt", "memory"):
        _need(raw, section, "config/settings.json")
    agent = raw["agent"]
    runtime = raw["runtime"]
    from core.orchestration.config import load_orchestration
    load_orchestration(raw)
    for key in ("name", "version", "max_model_steps", "max_attempts_per_step"):
        _need(agent, key, "config.agent")

    session_mode = runtime.get("session_mode", "queue")
    if session_mode not in ("queue", "interrupt"):
        raise ValueError(f"runtime.session_mode 非法：{session_mode}")
    steps = int(agent["max_model_steps"])
    attempts = int(agent["max_attempts_per_step"])
    if steps < 1:
        limit = raw.get("tools", {}).get("max_tool_rounds", 10)
        label = "无本地轮数上限" if limit is None else f"当前上限 {limit}"
        raise ValueError(
            "max_model_steps 必须 ≥1（R18/R22：语义为决策回合数，本阶段为 1；"
            f"tools.max_tool_rounds：{label}；缺键时才使用缺省值）")
    if not 1 <= attempts <= 4:
        
        raise ValueError("max_attempts_per_step 必须在 1~4（采纳件 §7：主 3 + 备 1）")


    import hashlib

    config_hash = hashlib.sha256(
        json.dumps(raw, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]

    return AgentDefinition(
        name=agent["name"],
        version=agent["version"],
        locale=agent.get("locale", "zh-CN"),
        owner=agent.get("owner", ""),
        max_model_steps=steps,
        max_attempts_per_step=attempts,
        recent_raw_turns=int(agent.get("recent_raw_turns", 20)),
        session_mode=session_mode,
        config_path=path.resolve(),
        config_version_hash=config_hash,
        raw=raw,
    )
