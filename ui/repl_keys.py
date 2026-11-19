"""按键语义（ReplKeys mixin；B40 职责拆分自 ui/repl.py 移出，行为不变）。

Ctrl+C 五语境分派（P0-1 接线，QD-011 口径；TASK-24/QD-115：询问态 = 拒绝该次工具确认）
+ 非模式语境 2.0s 连按强退窗口 + Enter 提交 / Ctrl+Enter（c-j）换行 / Ctrl+D 退出绑定。
ReplUI 本体只留输入循环、SIGINT 兜底与边界计时器。
"""

import time

from prompt_toolkit.key_binding import KeyBindings

_CTRL_C_FORCE_QUIT_WINDOW_S = 2.0   # QD-011：第二次连按强退窗口（非模式语境）


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

        # P0-1 接线（ISSUE-HM-010）：Ctrl+C 五语境分派（TASK-04 误删本绑定的教训）。
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
        """c-c 键绑定入口（五语境 + 强退窗口 + TASK-24 询问态）。

        QD-011 口径（交付报告同步声明）：强退窗口 = **2.0 秒**；
        适用语境 = **非模式语境的任意状态**（生成中/空闲有草稿/空闲无草稿均可连按强退）；
        /model 模式内的 Ctrl+C 用于回退模式，**不武装**强退计时（避免连按退出模式时误退程序）。
        TASK-24（QD-115）：询问态下 Ctrl+C = 拒绝该次工具确认（user_cancel），
        Run 继续（模型收到明确拒绝结果）；连按强退窗口照常武装。
        """
        from ui.session_commands import cancel_replay
        if cancel_replay(self):
            return
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
            # ISSUE-HM-023：真控制台 raw 模式下 Ctrl+C 只以 c-c 键事件到达（不产生
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
        """Ctrl+C 语境分派。模式内回退是基线 C3 四场景之外的第 5 语境，
        且**先于**其它场景被模式态吃掉（QD-005）：不清草稿、不弹 /exit 提示。
        TASK-24（QD-115）：询问态 Ctrl+C = 拒绝该次工具确认（先于"取消 Run"判定）。"""
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
