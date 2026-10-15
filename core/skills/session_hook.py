"""/skill 显式触发通道（V3②；D4 优先级最高）。宿主 = SessionCoordinator。

D10：禁止塞进 core/session.py 类体——本 Mixin 照 session_compact.py 先例外挂。
宿主须持有字段：_skills（SkillsContext | None，构造注入）。
_armed_skill 为类级默认 None；arm 后落实例；消费在 session.submit
（一次性，进 RunRequest.explicit_skill，随 Run 冻结）。
"""

from __future__ import annotations


class SkillSessionMixin:
    """/skill 命令的协调器侧：列举 / 安排显式触发 / 取消。"""

    _armed_skill: str | None = None

    def arm_skill(self, name: str) -> str:
        """为下一条输入安排显式触发（一次性；生效后自动失效）。"""
        skills = self._skills
        if skills is None:
            return "技能能力未装配"
        avail = skills.available()
        if name in avail:
            self._armed_skill = name
            return f"已安排技能「{name}」随下一条输入显式加载"
        faults = {f.skill_dir: f.reason for f in skills.unavailable()}
        if name in faults:
            return f"技能「{name}」不可用：{faults[name]}"
        known = "、".join(sorted(avail)) or "（无）"
        return f"技能「{name}」不存在或未启用；当前可用：{known}"

    def disarm_skill(self) -> str:
        """取消待触发的显式安排。"""
        if self._armed_skill is None:
            return "当前没有待触发的技能"
        name, self._armed_skill = self._armed_skill, None
        return f"已取消技能「{name}」的显式触发"

    def skill_status_lines(self) -> list[str]:
        """/skill（无参）：可用清单 + 未启用 + 显式不可用（含原因，禁静默）。"""
        skills = self._skills
        if skills is None:
            return ["技能能力未装配"]
        avail = skills.available()
        lines = ["可用技能："]
        if avail:
            lines += [f"  {meta.name} v{meta.version} —— {meta.description}"
                      for _, meta in sorted(avail.items())]
        else:
            lines.append("  （无）")
        off = skills.disabled()
        if off:
            lines.append("未启用（不在 skills.enabled 清单或元数据 enabled=false）：")
            lines += [f"  {name}" for name in off]
        faults = skills.unavailable()
        if faults:
            lines.append("不可用（显式）：")
            lines += [f"  {f.skill_dir} —— {f.reason}" for f in faults]
        if self._armed_skill:
            lines.append(f"待触发：{self._armed_skill}（随下一条输入生效）")
        return lines

    def _consume_armed_skill(self) -> str | None:
        """派发时取走待触发名（一次性）。"""
        name, self._armed_skill = self._armed_skill, None
        return name
