"""SkillsContext：装配期产物（main.py 构建；runtime / 协调器 / use_skill 工具共用）。

口径（skills-decisions v1.2）：
- D3 二级 AND 开关：可加载 ⟺ 元数据 enabled=true ∧ 名字在 skills.enabled 清单内
  （清单空 = 全关 = V6 判定路径）；
- D5 渐进披露：常驻上下文只放 name+description 清单；被选中才注入正文；
- D6 全量计 token：清单与正文都经 capability_note 进入 prepare_context 既有计量；
- C.4 诚实口径：记忆未启用 → memory_status() 显式 unavailable，不得伪造空结果；
- C.5：migrate() 为 B39 版本迁移预留接口（本批不实现）。
"""

from __future__ import annotations

import copy
from pathlib import Path

from core.skills.activation import CHANNEL_LABEL, ActivationGate, ActivationRecord
from core.skills.registry import Discovery, SkillFault, SkillMeta, discover

_MEMORY_NOTE = ("（本技能声明需要长期记忆；记忆能力当前不可用——"
                "不得伪造“查无记录”，涉及记忆时如实说明）")


class SkillsContext:
    """发现结果 + 开关配置 + 每 Run 一个 ActivationGate（run 间不残留）。"""

    def __init__(self, discovery: Discovery, enabled: list[str],
                 max_active: int = 1) -> None:
        self._discovery = discovery
        self._enabled = list(enabled)
        self._max_active = max(1, int(max_active))
        self._gate: ActivationGate | None = None
        self._memory = None
        self._releases = None
        self._run_discovery = None

    # ---- 装配期视图 ---------------------------------------------------------

    def available(self) -> dict[str, SkillMeta]:
        """二级 AND 开关后的可加载集（D3）。"""
        discovery = self._run_discovery or self._discovery
        discovery.unavailable[:] = [f for f in discovery.unavailable if not f.reason.startswith('live_release:')]
        result = {}
        for name, meta in discovery.available.items():
            if not (meta.enabled and name in self._enabled):
                continue
            try:
                if self._releases is not None:
                    self._releases.check_live(meta)
                result[name] = meta
            except Exception as exc:
                fault = SkillFault(name, 'live_release:' + str(exc))
                if fault not in discovery.unavailable:
                    discovery.unavailable.append(fault)
        return result

    def disabled(self) -> list[str]:
        """通过校验但被开关关掉的名（正常态，非故障）。"""
        return sorted(name for name, meta in self._discovery.available.items()
                      if not (meta.enabled and name in self._enabled))

    def active_names(self) -> frozenset[str]:
        """当前 Run 实际激活集，供工具层收紧工作流权限；不授予父层权限。"""
        return frozenset(self._gate.active) if self._gate else frozenset()

    def unavailable(self) -> list[SkillFault]:
        """显式不可用清单（发现期故障；D2 禁静默）。"""
        return list((self._run_discovery or self._discovery).unavailable)

    def catalog_note(self) -> str:
        """常驻清单（D5：只放 name+description，一行一条）。"""
        avail = self.available()
        if not avail:
            return ""
        lines = [f"- {meta.name}: {meta.description}"
                 for _, meta in sorted(avail.items())]
        return ("可用技能清单（常驻仅名称与说明；需要时经 use_skill 加载正文）：\n"
                + "\n".join(lines))

    def augment_note(self, note: str) -> str:
        """清单 + 已激活正文注入 capability_note（进既有 token 计量，D6）。"""
        parts = [note] if note else []
        catalog = self.catalog_note()
        if catalog:
            parts.append(catalog)
        injection = self.active_injection()
        if injection:
            parts.append(injection)
        return "\n".join(parts)

    def active_injection(self) -> str:
        """被选中技能的正文注入段（D5：只有 active 的才在这里出现）。"""
        if self._gate is None:
            return ""
        parts = []
        for rec in self._gate.active.values():
            meta = (self._run_discovery or self._discovery).available.get(rec.skill)
            if meta is None:
                continue
            if self._releases is not None:
                try:
                    self._releases.check_live(meta)
                except Exception:
                    parts.append(f"技能 {rec.skill} 不可用：来源已失效或依赖发生变化，须停止并重验。")
                    continue
            text = (f"已加载技能「{meta.name}」v{meta.version}"
                    f"（触发：{CHANNEL_LABEL.get(rec.trigger, rec.trigger)}）：\n{meta.body}")
            if meta.needs_memory:
                text += "\n" + self.memory_status()
            parts.append(text)
        return "\n\n".join(parts)

    # ---- 运行期（每 Run 一个门） ---------------------------------------------

    def begin_run(self, run_id: str, *, input_text: str = "",
                  explicit_skill: str | None = None, task_id: str | None = None) -> None:
        """开一个 Run 的激活门并处理显式/关键词通道（模型通道在 run 内经 use_skill）。

        评估顺序 = 关键词先、显式后：显式优先级更高，命中即替换并留痕（D4①）。
        """
        self._run_discovery = copy.deepcopy(self._discovery)
        if self._releases is not None:
            from core.skills.release_store import events
            if events(self._releases):
                self._run_discovery = self._releases.snapshot(task_id)
        self._gate = ActivationGate(self._max_active)
        avail = self.available()
        for hit in self._keyword_matches(avail, input_text):
            self._gate.activate(hit, "keyword")
        if explicit_skill:
            meta = avail.get(explicit_skill)
            if meta is not None:
                self._gate.activate(meta, "explicit")
            else:
                self._gate.records.append(ActivationRecord(
                    "rejected", explicit_skill, "", "", "", "explicit",
                    f"技能 {explicit_skill} 不可用或未启用"))

    def end_run(self) -> list[dict]:
        """收尾：返回本 Run 全量触发记录（冻结到 RunResult.skill_loads）。"""
        gate, self._gate = self._gate, None
        self._run_discovery = None
        if gate is None:
            return []
        return [rec.to_payload() for rec in gate.records]

    def drain_records(self) -> list[ActivationRecord]:
        """增量记录（事件发射用；仅 loaded/replaced/rejected 由调用方落成事件）。"""
        return [] if self._gate is None else self._gate.drain_records()

    def _keyword_matches(self, avail: dict[str, SkillMeta], text: str) -> list[SkillMeta]:
        """每个命中者均进激活门；超额者也有明确拒绝记录。"""
        if not text:
            return []
        low = text.lower()
        return [meta for _, meta in sorted(avail.items())
                if any(t and t.lower() in low for t in meta.triggers)]

    # ---- 模型通道（use_skill 工具桥，D7：同一闸门） ----------------------------

    def use_skill(self, name: str) -> str:
        """模型自主加载：返回技能正文（=工具结果回填模型）或显式拒绝文案。"""
        avail = self.available()
        meta = avail.get(name)
        if meta is None:
            known = "、".join(sorted(avail)) or "（无）"
            return f"技能不可用或不存在：{name or '（空）'}。当前可用技能：{known}"
        if self._gate is None:
            return "技能加载通道未就绪（不在 Run 内）"
        rec = self._gate.activate(meta, "model")
        if rec.action == "rejected":
            return rec.detail
        if rec.action == "already_active":
            return f"技能 {name} 本轮已加载，正文见前。"
        head = f"已加载技能「{name}」v{meta.version}"
        if rec.action == "replaced":
            head += f"（{rec.detail}）"
        body = meta.body + ("\n" + self.memory_status() if meta.needs_memory else "")
        return f"{head}：\n{body}"

    # ---- 诚实口径与预留 -------------------------------------------------------

    def memory_status(self) -> str:
        """D12/B26：按真实装配状态报告，能力可用不代表已获写入授权。"""
        if self._memory is None:
            return 'unavailable：依赖缺失。' + _MEMORY_NOTE
        return 'available：MemoryService已装配；归档须用户确认，读写工具仍受独立开关约束。'

    def bind_memory(self, memory):
        self._memory = memory

    def bind_releases(self, releases):
        self._releases = releases

    def migrate(self, *_args, **_kwargs):
        """B39 版本迁移预留接口（C.5：本批不实现）。"""
        raise NotImplementedError("B39 版本迁移预留接口：本批不实现")


def build_skills_context(config: dict, base_dir: Path, tool_registry) -> SkillsContext:
    """按 config 装配（config 只读）。skills.dir 默认 skills/；max_active_skills 默认 1。"""
    cfg = config.get("skills", {})
    skills_dir = Path(cfg.get("dir", "skills/"))
    if not skills_dir.is_absolute():
        skills_dir = base_dir / skills_dir
    discovery = discover(skills_dir, tool_registry)
    return SkillsContext(discovery, enabled=list(cfg.get("enabled", [])),
                         max_active=int(cfg.get("max_active_skills", 1)))
