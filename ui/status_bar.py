"""Vastar runtime component."""
from __future__ import annotations

import time

from ui.theme import DONE_SYMBOL, TIME_FRAMES, format_elapsed


def _fmt_tokens(n) -> str:
    if not isinstance(n, (int, float)):
        return "?"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def model_label(coord, renderer) -> str:
    """Vastar runtime component."""
    thinking = "开" if getattr(renderer, "thinking_enabled", True) else "关"
    model = str(coord.primary_label()).split("/", 1)[-1]
    return f"{model} · 思考{thinking}"


def context_label(coord) -> str:
    """Vastar runtime component."""
    last = getattr(coord, "last_result", None)
    estimate = None
    if last is not None:
        estimate = (getattr(last, "budget_summary", None) or {}).get("estimated_tokens")
    profile = getattr(getattr(coord, "_primary", None), "profile", None)
    total = getattr(profile, "context_window", None)
    return f"~{_fmt_tokens(estimate or 0)}/{_fmt_tokens(total)}"


def session_display_name(coord) -> str:
    """③ session 名：自定义名直接显示；无名字按建库顺序 `session 1` 起顺延。"""
    sid = str(getattr(coord, "session_id", "") or "")
    if sid and not sid.startswith("sess-"):
        return sid
    store = getattr(coord, "_store", None)
    if store is None:
        return "session 1"
    try:
        with store._lock:
            rows = store._conn.execute(
                "SELECT session_id FROM sessions ORDER BY rowid").fetchall()
        ids = [row[0] for row in rows]
    except (ValueError, KeyError, TypeError, AttributeError):
        return "session 1"   # 与 status_view 可选节同口径：展示降级，不阻断输入
    if sid in ids:
        return f"session {ids.index(sid) + 1}"
    return f"session {len(ids) + 1}"


def time_status(coord, *, now: float | None = None, last_seconds: float | None = None) -> str:
    """④ 时间状态栏：执行中 `. o O` 渐变动画＋本轮耗时；完成后 `√`＋上轮耗时。"""
    now = time.monotonic() if now is None else now
    active = getattr(coord, "_active", None)
    progress = getattr(coord, "_live_progress", None)
    if active is not None and progress and progress.get("run_id") == active.run_id:
        frame = TIME_FRAMES[int(now * 3) % len(TIME_FRAMES)]
        return f"{frame} {format_elapsed(now - progress['started'])}"
    return f"{DONE_SYMBOL} {format_elapsed(last_seconds or 0)}"


def build_status_line(coord, renderer, *, permission_mode: str | None = None,
                      now: float | None = None) -> str:
    """六段信息行：权限 / 模型 / 占用 / session / 时间 / 心情（只显示生效态）。"""
    mood = getattr(renderer, "mood", None)
    mood_text = mood.label if mood is not None else "Calm(-_-)"
    return " │ ".join((
        permission_mode or "ask",
        model_label(coord, renderer),
        context_label(coord),
        session_display_name(coord),
        time_status(coord, now=now, last_seconds=getattr(renderer, "last_run_seconds", None)),
        mood_text,
    ))
