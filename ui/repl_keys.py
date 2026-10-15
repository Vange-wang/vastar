"""Vastar runtime component."""

import time

from prompt_toolkit.key_binding import KeyBindings

_CTRL_C_FORCE_QUIT_WINDOW_S = 2.0   


class ReplKeys:
    """键绑定与 Ctrl+C 语境分派（mixin；状态字段由 ReplUI.__init__ 初始化）。"""

    def _build_bindings(self) -> KeyBindings:
        kb = KeyBindings()
        coord = self._coordinator

        @kb.add("enter")
        def _submit(event):  # noqa: ANN001
            event.current_buffer.validate_and_handle()

        # Windows 上 Ctrl+Enter 经 win32 后端到达为 ControlJ（uChar='\n'）：
        # 这是 prompt_toolkit 的原生映射，零补丁直接绑定 c-j。
        @kb.add("c-j")
        def _newline(event):  # noqa: ANN001
            event.current_buffer.insert_text("\n")

        
        @kb.add("c-c")
        def _ctrl_c(event):  # noqa: ANN001
            self._on_ctrl_c(bool(event.current_buffer.text), event)

        @kb.add("c-d")
        def _eof(event):  # noqa: ANN001
            if not event.current_buffer.text and not coord.is_busy():
                self._exit_requested = True
                event.current_buffer.validate_and_handle()

        return kb

    def _on_ctrl_c(self, has_draft: bool, event=None) -> None:
        """Vastar runtime component."""
        now = time.monotonic()
        was_mode = self._model_mode
        if not was_mode and (now - self._last_ctrl_c) <= _CTRL_C_FORCE_QUIT_WINDOW_S:
            self._force_quit()
            return
        action = self._ctrl_c_semantics(has_draft)
        self._last_ctrl_c = 0.0 if was_mode else now
        if action == "exit_mode":
            self._renderer.render_note("已退出模型选择")
        elif action == "cancel_ask":
            self._renderer.render_note("已拒绝本次工具确认（Ctrl+C）；当前回答继续")
        elif action == "cancel_run":
            
            # SIGINT），本分支是生成中取消的**唯一生效路径**——此前只提示不取消，
            # “已请求取消当前回答…”成了假提示（实机 FAIL 根因）。
            self._coordinator.request_cancel()
            self._renderer.render_note("已请求取消当前回答…")
        elif action == "clear_draft":
            if event is not None:
                event.current_buffer.text = ""
            self._renderer.render_note("草稿已清空")
        elif action == "exit_hint":
            if not self._ctrl_c_announced:
                self._renderer.render_note(
                    "空闲且无草稿；输入 /exit 退出程序（2s 内再按 Ctrl+C 强制退出）")
                self._ctrl_c_announced = True
        # clear_draft / cancel_run 后进入各自语境；连按窗口由 _last_ctrl_c 承接

    def _force_quit(self) -> None:
        self._renderer.render_note("已强制退出（2s 内第二次 Ctrl+C）")
        self._exit_requested = True
        self._coordinator.stop()

    def _ctrl_c_semantics(self, has_draft: bool) -> str:
        """Vastar runtime component."""
        if self._model_mode:
            self._model_mode = False
            return "exit_mode"
        if self._ask_gate is not None and self._ask_gate.cancel():
            return "cancel_ask"
        if self._coordinator.has_active_or_pending():
            return "cancel_run"
        if has_draft:
            return "clear_draft"
        return "exit_hint"
