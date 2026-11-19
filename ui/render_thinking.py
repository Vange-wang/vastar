"""思考折叠呈现（RenderThinking mixin；B40 职责拆分自 ui/renderer.py 移出，行为不变）。

R2 裁定：思考以**滚动窗口**显示（状态头 + 分隔线 + 省略行 + 最近 N 行，块高恒 ≤N+3=8，
窗口拼装见 ui/thinking_window.py）；结束即按**实际块高**整块上擦——擦除有界、折叠必成。
/think toggle：展开上一轮全文 / 收起恢复折叠行（诚实边界：展开期间有新输出则不硬擦）。
Renderer 本体只留事件分发与通道原语；本 mixin 只管思考通道的折叠呈现。
"""

from __future__ import annotations

from ui.thinking_window import ThinkingWindow


def _collapsed_text(n: int) -> str:
    """思考折叠行文案（_collapse_reasoning / toggle_think 共用，KM-23）。"""
    return f"▏思考 · 已折叠（{n} 字，/think 查看全文）"


def _fulltext_header(n: int) -> str:
    """思考全文头文案（render_reasoning_full / toggle_think 共用，KM-23）。"""
    return f"▏思考 · 上一轮全文（{n} 字）"


class RenderThinking:
    """R2 思考折叠：滚动窗口重绘 / 结束折叠 / /think 全文 toggle（状态字段由 Renderer.__init__ 初始化）。"""

    @property
    def last_reasoning(self) -> str | None:
        """/think 数据源：上一轮思考全文（折叠时捕获；裁定 R2，屏幕层面）。"""
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
        """滚动窗口整块重绘（HM-034）：按上次块高擦除 → 重画最新窗口块 → 批尾补边界换行。
        擦+画+换行为一次原子写（R9）。TASK-116 §9：状态头符号逐帧转动（一／|／/／\\），
        宽度每次重绘实时重测（resize 不吃旧宽度）。
        """
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
        """思考结束折叠（HM-031/HM-034）：按**实际块高**（恒 ≤8）整块上擦，写折叠行。
        块高有界 → 折叠必成；"过长即不擦"的降级路径（旧 _block_exceeds_screen）已随
        滚动窗口废除——折叠判定不再消费终端行数（屏高取值可靠性问题在方案层消解）。"""
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
        """/think：屏幕层面重新展示上一轮思考全文（数据库本就存全文，裁定 R2）。"""
        self._close_open_channel()
        self._emit("style.reasoning", _fulltext_header(len(text)) + "\n", flush=True)
        for line in text.splitlines() or [""]:
            self._emit("style.reasoning", f"  {line}\n", flush=True)

    def toggle_think(self) -> str | None:
        """/think 开关：未展开 → 打印全文并记录可视行数；已展开 → 收起（恢复折叠行）。
        诚实边界（裁定 R2 v1.1）：展开期间已有新输出时，不硬擦，返回提示文案
        （P0-9.2：无新输出时必须能收起——计数器由 _emit 全量维护，状态不漏复位）。
        返回 None 表示已直接呈现。"""
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
