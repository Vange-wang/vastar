"""Vastar runtime component."""

from __future__ import annotations

from core.errors import ErrorCode, VastarError


class ModelSwitchMixin:
    """模型目录与切换；宿主为 SessionCoordinator。"""

    def model_catalog(self) -> list[tuple[int, str, str, bool]]:
        """Vastar runtime component."""
        current = (self._primary.name, self._primary.model)
        return [
            (i, p, m, (p, m) == current)
            for i, (p, m) in enumerate(self._model_catalog, start=1)
        ]

    def output_limit_label(self) -> str:
        """Vastar runtime component."""
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
        
        others = [(p, m) for p, m in self._model_catalog if p != provider]
        new_fallback = self._spec_factory(others[0][0], others[0][1]) if others else None
        self._primary = new_primary
        self._fallback = new_fallback
        return True, f"模型已切到 {provider}/{model}（仅对之后的请求生效）"

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
