"""Run 生命周期呈现，外提以保持 Renderer 余量。"""
from ui.usage_view import run_usage


class RenderLifecycle:
    def _render_attempt(self, event) -> None:
        p = event.payload
        provider = str(p.get("provider", ""))
        index = int(p.get("attempt_index", 1))
        is_switch = self._last_provider is not None and provider != self._last_provider
        self._last_provider = provider
        self._close_open_channel()
        if p.get("kind") == "continuation":

            limit = (p.get("request_params") or {}).get("max_tokens", "?")
            self._emit("style.warning",
                       f"⚠ 已达单次输出上限（max_tokens={limit}），"
                       f"正在自动续写 {p.get('continuation_index')}"
                       f"/{p.get('continuation_max') if p.get('continuation_max') is not None else '不限'}…\n", flush=True)
        elif is_switch:
            self._emit("style.warning",
                       f"[切备] {provider} · {p.get('model')}\n", flush=True)
        elif index > 1:
            self._emit("style.warning",
                       f"[重试 第{index}次] {provider} · {p.get('model')}\n", flush=True)
        else:
            self._emit("style.process",
                       f"[{provider} · {p.get('model')}] 请求中…\n", flush=True)

    def _render_run_ended(self, event) -> None:
        from ui.renderer import _PLAIN, _incomplete_text

        self._close_open_channel()
        self._last_provider = None  # 新 Run 从主 provider 重新开始
        self._think_expanded = False  # 新 Run 复位 /think 展开态（P0-9.2：状态不漏复位）
        self._think_writes_since_expand = 0
        p = event.payload
        status = str(p.get("status", ""))
        usage = p.get("usage_totals") or {}
        latencies = p.get("latencies") or {}
        total_ms = latencies.get("submit_to_terminal_result", 0) / 1_000_000
        self.last_run_seconds = total_ms / 1000   # §6④ 时间栏 `√` 后的上轮耗时
        self._ui_dirty = True                     # 状态行转入完成态，请求重绘
        degraded = p.get("degraded", False)
        if status == "completed":
            head = self._styled("style.status", "[回答完成；任务待验收]"
                                if p.get("task_id") and p.get("task_status") != "COMPLETED" else "[完成]")
        elif status == "cancelled":
            head = self._styled("style.warning", "[已取消]")
        elif status == "incomplete":

            head = self._styled("style.warning", _incomplete_text(p))
        else:
            head = self._styled("style.error", f"[失败] {p.get('finish_reason', '')}")
        suffix = ""
        if degraded:

            fallback_label = p.get("degraded_to") or "备用模型"
            suffix = f" · 已降级 → 备用 {fallback_label}"
        explanation = run_usage(usage, p.get('call_records') or [], p.get('model_calls', 0))
        token_part = f" · token {explanation}" if explanation else ""
        self._emit(_PLAIN, f"{head}{suffix} · {total_ms / 1000:.1f}s{token_part}\n",
                   flush=True)
        note = (p.get('result') or {}).get('terminal_note', '')
        if note:
            self._emit('style.warning', note + '\n', flush=True)

