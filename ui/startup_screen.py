"""Pre-rendered local terminal splash; no image libraries or network at launch."""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading
import zlib
from pathlib import Path

_CACHE = Path(__file__).parent / "assets" / "startup" / "wallpaper.frames.zlib"
_MAINTENANCE = {
    "-h", "--help", "--show-paths", "--init-project", "--migrate-from",
    "--apply-migration", "--migrate-storage", "--apply-storage-migration",
}
_active = None


def frames_for_terminal(cache, columns, lines):
    width, height = columns - 4, lines - 8
    if width < 40 or height < 7:
        return []
    sizes = sorted(cache["sizes"], key=lambda item: item["width"])
    item = next((item for item in sizes if item["width"] >= width), sizes[-1])
    width, height = min(width, item["width"]), min(height, item["height"])
    left = (item["width"] - width) // 2
    logo_left, logo_width = item.get("logo_left", 0), item.get("logo_width", 0)
    if logo_width:
        left = max(0, min(item["width"] - width,
                          max(logo_left + logo_width - width, min(left, logo_left))))
    logo_top = item.get("logo_top", round(item["height"] * 0.34))
    top = max(0, min(item["height"] - height, round(logo_top + (7 - height) / 2)))
    frames = item.get("frames")
    if frames is None:
        raw = (_CACHE.parent / item["file"]).read_bytes()
        frames = json.loads(zlib.decompress(raw))["frames"]
    return ["\n".join(crop_ansi_row(row, left, width)
                      for row in frame.splitlines()[top:top + height])
            for frame in frames]


def crop_ansi_row(row, left, width):
    """Crop cached cells, retaining color state; leave two columns at either edge."""
    start, end = left + 2, left + 2 + width
    parts, position = ["\x1b[0m  "], 0
    for token in re.split(r"(\x1b\[[0-9;]*[mK])", row):
        if token.startswith("\x1b"):
            parts.append(token)
            continue
        begin, finish = max(0, start - position), min(len(token), end - position)
        if begin < finish:
            parts.append(token[begin:finish])
        position += len(token)
        if position >= end:
            break
    return "".join(parts) + "\x1b[0m\x1b[K"


class StartupScreen:
    """Owns a bounded animation only during loading; leaves the final frame in scrollback."""

    def __init__(self, output, frames):
        self.output, self.frames = output, frames
        self._stop = threading.Event()
        self._thread = None
        self._height = frames[0].count("\n") + 1
        self._size = shutil.get_terminal_size()

    def start(self):
        self.output.write(self.frames[0] + "\n")
        self.output.flush()
        self._thread = threading.Thread(target=self._animate, daemon=True,
                                        name="vastar-startup-stars")
        self._thread.start()
        return self

    def _animate(self):
        index = 0
        while not self._stop.wait(0.3):
            # Don't repaint an old-sized region after the user resizes the terminal.
            if shutil.get_terminal_size() != self._size:
                return
            index = (index + 1) % len(self.frames)
            try:
                self.output.write(f"\x1b[{self._height}A\r" + self.frames[index] + "\n")
                self.output.flush()
            except (OSError, ValueError):
                return

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()


def start_startup(argv, *, output=None):
    global _active
    output = sys.stdout if output is None else output
    if _active is not None or not output.isatty():
        return _active
    if any(arg.split("=", 1)[0] in _MAINTENANCE for arg in argv):
        return None
    if "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb":
        return None
    try:
        if os.name == "nt":
            os.system("")  # Enable Windows VT before the first cached color frame.
        size = shutil.get_terminal_size()
        cache = json.loads(zlib.decompress(_CACHE.read_bytes()))
        frames = frames_for_terminal(cache, size.columns, size.lines)
        if frames:
            _active = StartupScreen(output, frames).start()
    except (OSError, ValueError, KeyError, zlib.error):
        # Cosmetic asset failure must never prevent launching the agent.
        return None
    return _active


def stop_startup():
    global _active
    if _active is not None:
        _active.stop()
        _active = None
