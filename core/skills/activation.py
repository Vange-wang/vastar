"""三通道触发与超额处置（skills-decisions v1.2 D4；TASK-2026-10-02-46）。

优先级定死：显式(3) > 关键词(2) > 模型(1)。同一轮最多 max_active 个（默认 1）：
- 新请求优先级更高 → 替换当前最低优先级者（记审计，action=replaced）；
- 与当前同级 → 拒绝并提示下一轮重新触发（不承诺不存在的自动排队）；
- 冲突/重复只加载一次（action=already_active）。
每条决策都产 ActivationRecord（含来源/hash/版本/触发原因）——审计与 Run 冻结共用。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

CHANNEL_PRIORITY = {"model": 1, "keyword": 2, "explicit": 3}
CHANNEL_LABEL = {"explicit": "显式", "keyword": "关键词", "model": "模型"}


@dataclass
class ActivationRecord:
    """一次触发决策的留痕（D8 审计事件 payload 与 RunResult.skill_loads 同源）。"""

    action: str      # loaded / replaced / rejected / already_active
    skill: str
    version: str
    sha256: str
    source: str
    trigger: str     # explicit / keyword / model
    detail: str = ""

    def to_payload(self) -> dict:
        return {"action": self.action, "skill": self.skill, "version": self.version,
                "sha256": self.sha256, "source": self.source, "trigger": self.trigger,
                "trigger_label": CHANNEL_LABEL.get(self.trigger, self.trigger),
                "detail": self.detail}


class ActivationGate:
    """一个 Run 的激活门（每 Run 新实例；run 间不残留）。"""

    def __init__(self, max_active: int = 1) -> None:
        self.max_active = max(1, int(max_active))
        self.active: dict[str, ActivationRecord] = {}
        self.records: list[ActivationRecord] = []
        self._drained = 0

    def activate(self, meta, channel: str) -> ActivationRecord:
        if meta.name in self.active:
            active = self.active[meta.name]
            if CHANNEL_PRIORITY[channel] > CHANNEL_PRIORITY[active.trigger]:
                self.active[meta.name] = replace(active, trigger=channel)
            return self._record(ActivationRecord(
                "already_active", meta.name, meta.version, meta.sha256, meta.source,
                channel, "本轮已加载，重复触发只加载一次"))
        if len(self.active) < self.max_active:
            rec = ActivationRecord("loaded", meta.name, meta.version, meta.sha256,
                                   meta.source, channel)
            self.active[meta.name] = rec
            return self._record(rec)
        weakest = min(self.active.values(), key=lambda r: CHANNEL_PRIORITY[r.trigger])
        new_prio, cur_prio = CHANNEL_PRIORITY[channel], CHANNEL_PRIORITY[weakest.trigger]
        if new_prio > cur_prio:
            del self.active[weakest.skill]
            rec = ActivationRecord(
                "replaced", meta.name, meta.version, meta.sha256, meta.source, channel,
                f"替换 {weakest.skill}（{CHANNEL_LABEL[weakest.trigger]}通道，优先级更低）")
            self.active[meta.name] = rec
            return self._record(rec)
        detail = (f"本轮已加载 {self.max_active} 个技能（当前：{weakest.skill}），"
                  f"{meta.name} 未加载，请在下一轮重新触发" if new_prio == cur_prio else
                  f"本轮已加载 {weakest.skill}（{CHANNEL_LABEL[weakest.trigger]}通道，"
                  f"优先级更高），{meta.name} 未加载")
        return self._record(ActivationRecord(
            "rejected", meta.name, meta.version, meta.sha256, meta.source, channel, detail))

    def drain_records(self) -> list[ActivationRecord]:
        """增量取走新记录（事件发射用；全量记录仍留在 records 供 Run 冻结）。"""
        new = self.records[self._drained:]
        self._drained = len(self.records)
        return new

    def _record(self, rec: ActivationRecord) -> ActivationRecord:
        self.records.append(rec)
        return rec
