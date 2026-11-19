"""ModelSwitchMixin：/model 会话级切换（裁定 2026-09-20-model-switch-and-banner_hm）。

从 SessionCoordinator 拆出（铁律 V6：单类 ≤300 行）。语义（裁定 §二.3，写死不可放宽）：
- 会话级临时切换，不回写 config/settings.json（铁律 V1）；
- 只对之后的请求生效：在途/已完成 Run 的 attempt 快照不受影响；
- 参数/输出预留/上下文预算按新 provider 重算（与降级同一套逻辑）；
- 忙碌中拒绝；不自动路由（纯人工切换）。

宿主（SessionCoordinator）需持有：_primary / _fallback / _model_catalog /
_spec_factory / _active 属性。
"""

from __future__ import annotations

from core.errors import ErrorCode, VastarError


class ModelSwitchMixin:
    """模型目录与切换；宿主为 SessionCoordinator。"""

    def model_catalog(self) -> list[tuple[int, str, str, bool]]:
        """可选模型清单：[(序号, provider, model, 是否当前)]。

        序号按 config/settings.json 注册表声明顺序 1 起编，**永不重排**
        （TASK-03 P0-B：顺序漂移会让用户记错序号）。
        """
        current = (self._primary.name, self._primary.model)
        return [
            (i, p, m, (p, m) == current)
            for i, (p, m) in enumerate(self._model_catalog, start=1)
        ]

    def output_limit_label(self) -> str:
        """当前主 provider 的单次输出上限（/status 与欢迎行可见性，TASK-34 B②）。"""
        return str(self._primary.params.get("max_tokens", "?"))

    def switch_model(self, target: str) -> tuple[bool, str]:
        """切换当前会话使用的模型。返回 (是否成功, 提示文案)。

        接受三种写法：序号（如 "2"）/ 裸 model-id / "provider/model"（向后兼容）。
        """
        if self._active is not None:
            return False, "正在回答中，/model 稍后再试（可先 Ctrl+C 取消当前回答）"
        resolved = self._resolve_model(target.strip())
        if resolved is None:
            total = len(self._model_catalog)
            return False, (
                f"无法识别：{target}"
                + (f"（序号有效范围 1~{total}；也可写模型名或 provider/model，/model 查看）"
                   if total else "（无可选模型）"))
        provider, model = resolved
        if (provider, model) == (self._primary.name, self._primary.model):
            return True, f"当前已在使用 {provider}/{model}，无需切换"
        try:
            new_primary = self._spec_factory(provider, model)
        except VastarError as err:
            if err.code == ErrorCode.AUTH_ERROR:
                return False, f"无法切换到 {provider}/{model}：{err.message}"
            raise
        # KM-04：先构造两份 spec 再一次性提交（避免"主模型已切但备用重建失败"）
        others = [(p, m) for p, m in self._model_catalog if p != provider]
        new_fallback = self._spec_factory(others[0][0], others[0][1]) if others else None
        self._primary = new_primary
        self._fallback = new_fallback
        message = f"模型已切到 {provider}/{model}（仅对之后的请求生效）"
        tools = getattr(self, "task_tools_context", None)
        if getattr(tools, "team_entry_config", None):
            from core.llm.pricing import require_pricing
            try:
                require_pricing(new_primary.pricing, provider, model)
            except ValueError as exc:
                message += f"；普通对话可用，任务暂不可用：{exc}"
        return True, message

    def _resolve_model(self, target: str) -> tuple[str, str] | None:
        """序号 → 按目录定位；名字 → 唯一匹配。解析失败返回 None。"""
        if target.isdigit():
            n = int(target)
            if 1 <= n <= len(self._model_catalog):
                p, m = self._model_catalog[n - 1]
                return p, m
            return None
        if "/" in target:
            p, _, m = target.partition("/")
            p, m = p.strip(), m.strip()
            for cp, cm in self._model_catalog:
                if cp == p and cm == m:
                    return cp, cm
            return None
        matches = [(cp, cm) for cp, cm in self._model_catalog if cm == target]
        if len(matches) == 1:
            return matches[0]
        return None
