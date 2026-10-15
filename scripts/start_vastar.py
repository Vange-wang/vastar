"""Canonical launch bootstrap: draw cached artwork before importing the agent."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ui.startup_screen import start_startup, stop_startup  # noqa: E402


def launch():
    start_startup(sys.argv[1:])
    try:
        from main import main
        return main()
    finally:
        stop_startup()


if __name__ == "__main__":
    raise SystemExit(launch())
