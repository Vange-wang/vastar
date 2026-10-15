"""Vastar runtime component."""

from __future__ import annotations

HEADER = "▏思考中~~~"
SEPARATOR = "-" * 18   
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
    """滚动窗口构成器：全文 → 屏幕块（行列表，高 ≤ visible_lines + 3）。"""

    def __init__(self, visible_lines: int = 5) -> None:
        self._n = max(1, visible_lines)

    @property
    def max_block_lines(self) -> int:
        """块高硬上界：状态头 + 分隔线 + 省略行 + N 内容行（默认 8）。"""
        return self._n + 3

    def compose(self, full_text: str, width: int) -> list[str]:
        """构成当前应显示的块：窗口内恒为**最近** N 行（行序语义：新行入、顶行出）。"""
        lines = wrap_visible_lines(full_text, width)
        block = [HEADER, SEPARATOR]
        if not lines:
            return block
        if len(lines) > self._n:
            block.append(ELLIPSIS)
        block.extend(lines[-self._n:])
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
