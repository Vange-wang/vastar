"""思考滚动窗口（TASK-12 / ISSUE-HM-034；TASK-116 §9 改形）。

TASK-116 形态（块高恒定 ≤ visible_lines + 4，默认 ≤9）：

    ------------------    ← 顶线（随终端宽度）
    一 Thinking......      ← 状态头：符号处转动（一／|／/／\\ 循环，弱化样式）
    …                     ← 省略行：上面还有已滚出的思考（不足 N 行时不显示，诚实边界）
    <最近 N 行内容>        ← 滚动窗口：新行入 → 最上一行出；全文不受限（/think 照旧）
    ------------------    ← 底线：上下两条线夹住输出（§9）

块高有界 → 折叠擦除量恒 ≤N+4 行、永不触到终端屏顶 → **折叠必成**（出正文即折叠，
与现行 /think 折叠机制一致）。Renderer 只负责擦/写时机；行折行、窗口构成、
擦除序列构造全部在本模块（V6 规模红线：逻辑外置）。
"""

from __future__ import annotations

from ui.theme import SPINNER_FRAMES

ELLIPSIS = "…"


def wrap_visible_lines(text: str, width: int) -> list[str]:
    """全文 → 可视行列表：按 \\n 分逻辑行，再按终端宽度延迟折行（CJK 计宽 2）。

    末尾未完成的部分行保留（流式增长中它也是窗口的一行）；
    文本以 \\n 结尾时 split 产生的尾部空串不是内容行，剔除。
    """
    if not text:
        return []
    from prompt_toolkit.utils import get_cwidth

    lines: list[str] = []
    for logical in text.split("\n"):
        current = ""
        col = 0
        for ch in logical:
            w = max(1, get_cwidth(ch))
            if col + w > width and current:
                lines.append(current)
                current = ""
                col = 0
            current += ch
            col += w
        lines.append(current)
    if text.endswith("\n") and lines and lines[-1] == "":
        lines.pop()
    return lines


class ThinkingWindow:
    """滚动窗口构成器：全文 → 屏幕块（行列表，高 ≤ visible_lines + 4）。"""

    def __init__(self, visible_lines: int = 5) -> None:
        self._n = max(1, visible_lines)

    @property
    def max_block_lines(self) -> int:
        """块高硬上界：顶线 + 状态头 + 省略行 + N 内容行 + 底线（默认 9）。"""
        return self._n + 4

    def compose(self, full_text: str, width: int, *, frame: int = 0) -> list[str]:
        """构成当前应显示的块：窗口内恒为**最近** N 行；状态头符号按 frame 转动。"""
        width = max(4, width)
        lines = wrap_visible_lines(full_text, width)
        spinner = SPINNER_FRAMES[frame % len(SPINNER_FRAMES)]
        block = ["─" * width, f"{spinner} Thinking......"]
        if lines:
            if len(lines) > self._n:
                block.append(ELLIPSIS)
            block.extend(lines[-self._n:])
        block.append("─" * width)
        return block

    @staticmethod
    def erase_sequence(block_lines: int, *, cursor_below_block: bool) -> str:
        """按**实际块高**构造上擦序列（有界 ≤ block_lines 行，永不按全量思考行数）。

        cursor_below_block=True：边界计时器已补过换行，光标停在块下方空行行首，
        需多上移一行（清掉该空行后逐行清完整个块）；False：光标在块末行（部分行）上。
        """
        if block_lines <= 0:
            return ""
        ups = block_lines if cursor_below_block else block_lines - 1
        return "\r\x1b[2K" + "\x1b[1A\x1b[2K" * ups
