"""Vastar runtime component."""

from __future__ import annotations

from ui.thinking_window import ThinkingWindow


def _collapsed_text(n: int) -> str:
    """Vastar runtime component."""
    return f"▏思考 · 已折叠（{n} 字，/think 查看全文）"


def _fulltext_header(n: int) -> str:
    """Vastar runtime component."""
    return f"▏思考 · 上一轮全文（{n} 字）"


class RenderThinking:
    """R2 思考折叠：滚动窗口重绘 / 结束折叠 / /think 全文 toggle（状态字段由 Renderer.__init__ 初始化）。"""

    @property
    def last_reasoning(self) -> str | None:
        """Vastar runtime component."""
        return self._last_reasoning

    def _rv_feed(self, text: str) -> None:
        """按终端宽度跟踪思考块占用的可视行数（含换行与 CJK 宽度，延迟折行语义）。"""
        from prompt_toolkit.utils import get_cwidth

        for ch in text:
            if ch == "\n":
                self._rv_lines += 1
                self._rv_col = 0
                continue
            w = max(1, get_cwidth(ch))
            if self._rv_col + w > self._rv_width:
                self._rv_lines += 1
                self._rv_col = 0
            self._rv_col += w

    def _rv_repaint(self) -> None:
        """Vastar runtime component."""
        from ui.renderer import _terminal_width  # 延迟导入：renderer 反向继承本 mixin
        from ui.theme import SPINNER_FRAMES

        self._rv_width = _terminal_width()
        erase = ThinkingWindow.erase_sequence(
            self._rv_block_lines, cursor_below_block=self._at_line_start)
        block = self._thinking_window.compose(self._rv_text, self._rv_width,
                                              frame=self._rv_frame)
        self._rv_frame = (self._rv_frame + 1) % len(SPINNER_FRAMES)
        self._emit("style.reasoning", erase + "\n".join(block) + "\n", flush=True)
        self._rv_block_lines = len(block)
        self._rv_dirty = False

    def _collapse_reasoning(self) -> None:
        """Vastar runtime component."""
        full_text = self._rv_text
        self._last_reasoning = full_text if full_text else None
        if not full_text:
            self._rv_block_lines = 0
            self._rv_dirty = False
            return
        collapsed = _collapsed_text(len(full_text))
        erase = ThinkingWindow.erase_sequence(
            self._rv_block_lines, cursor_below_block=self._at_line_start)
        self._emit("style.reasoning", erase + collapsed + "\n", flush=True)
        self._rv_block_lines = 0
        self._rv_dirty = False
        self._rv_text = ""

    def render_reasoning_full(self, text: str) -> None:
        """Vastar runtime component."""
        self._close_open_channel()
        self._emit("style.reasoning", _fulltext_header(len(text)) + "\n", flush=True)
        for line in text.splitlines() or [""]:
            self._emit("style.reasoning", f"  {line}\n", flush=True)

    def toggle_think(self) -> str | None:
        """Vastar runtime component."""
        from ui.renderer import _terminal_width  # 延迟导入：renderer 反向继承本 mixin

        if not self._think_expanded:
            if not self._last_reasoning:
                return "上一轮没有可查看的思考内容"
            self._close_open_channel()
            self._rv_width = _terminal_width()
            self._rv_lines = 0
            self._rv_col = 0
            self._rv_feed(_fulltext_header(len(self._last_reasoning)) + "\n")
            self._rv_feed(self._last_reasoning + "\n")
            self._think_rv_lines = self._rv_lines
            self._think_rv_col = self._rv_col
            self.render_reasoning_full(self._last_reasoning)
            self._think_expanded = True
            self._think_writes_since_expand = 0
            return None
        # 已展开 → 收起
        if self._think_writes_since_expand > 0 or not self._at_line_start:
            return "展开期间已有新输出，无法原样收起"
        erase = ThinkingWindow.erase_sequence(self._think_rv_lines,
                                              cursor_below_block=True)
        collapsed = _collapsed_text(len(self._last_reasoning or ''))
        self._emit("style.reasoning", erase + collapsed + "\n", flush=True)
        self._think_expanded = False
        return None
