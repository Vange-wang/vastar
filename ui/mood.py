"""Vastar runtime component."""
from __future__ import annotations

MARK = "[[mood:"
MAX_MARKER_CHARS = 64      # 超过即判定非标记，按原文放行（防无限缓冲）
WORD_HARD_CAP = 15
KAOMOJI_CAP = 16
DEFAULT_MOOD = ("Calm", "(-_-)")


def _clean(text: str, cap: int) -> str:
    filtered = "".join(ch for ch in text if ch.isprintable() and ch not in "[]|")
    return filtered.strip()[:cap]


class MoodParser:
    """单通道流式解析器：feed(delta) -> (可视文本, [(词, 颜文字), ...])。"""

    def __init__(self) -> None:
        self._pending = ""

    def feed(self, text: str) -> tuple[str, list[tuple[str, str]]]:
        data = self._pending + text
        self._pending = ""
        out: list[str] = []
        moods: list[tuple[str, str]] = []
        pos = 0
        while pos < len(data):
            start = data.find("[[", pos)
            if start < 0:
                out.append(data[pos:])
                break
            out.append(data[pos:start])
            tail = data[start:]
            if len(tail) < len(MARK):
                if MARK.startswith(tail):
                    self._pending = tail          # 可能是标记开头，等下一片
                else:
                    out.append(tail)              # 只是普通 `[[`
                break
            if not tail.startswith(MARK):
                out.append("[[")
                pos = start + 2
                continue
            end = tail.find("]]", len(MARK))
            if end < 0:
                if len(tail) > MAX_MARKER_CHARS:
                    out.append(tail[:MAX_MARKER_CHARS])   # 畸形：原文放行
                    pos = start + MAX_MARKER_CHARS
                else:
                    self._pending = tail          # 未闭合且未超长，等下一片
                break
            body = tail[len(MARK):end]
            pos = start + end + 2
            word, sep, kaomoji = body.partition("|")
            word, kaomoji = _clean(word, WORD_HARD_CAP), _clean(kaomoji, KAOMOJI_CAP)
            if sep and word and kaomoji:
                moods.append((word, kaomoji))
            else:
                out.append(tail[:end + 2])        # 结构不完整：原文显示
        return "".join(out), moods

    def flush_pending(self) -> str:
        """Run 结束/通道关闭时吐出未闭合残片（不静默丢字）。"""
        pending, self._pending = self._pending, ""
        return pending


class MoodState:
    """最新心情（渲染只读；更新即置脏，驱动状态行重绘）。"""

    def __init__(self) -> None:
        self.current = DEFAULT_MOOD
        self.dirty = False

    def update(self, moods: list[tuple[str, str]]) -> None:
        if moods:
            self.current = moods[-1]
            self.dirty = True

    @property
    def label(self) -> str:
        word, kaomoji = self.current
        return f"{word}{kaomoji}"

    def consume_dirty(self) -> bool:
        dirty, self.dirty = self.dirty, False
        return dirty
