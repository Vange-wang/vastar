"""Non-destructive rotation. Retired segments are pruned only by confirmed maintenance."""

import json
import logging
import os
import time
import uuid
from pathlib import Path

DEFAULT = Path(__file__).with_name("maintenance-policy.json")


def policy():
    values = json.loads(DEFAULT.read_text(encoding="utf8"))
    override = os.environ.get("VASTAR_MAINTENANCE_POLICY")
    if override:
        extra = json.loads(Path(override).read_text(encoding="utf-8-sig"))
        if extra.keys() - values.keys():
            raise ValueError("unknown maintenance policy keys")
        values.update(extra)
    if any(type(n) is not int or n < 1 for n in values.values()):
        raise ValueError("maintenance limits must be positive integers")
    return values


class SegmentHandler(logging.FileHandler):
    def __init__(self, filename, limits=None):
        self.limits = limits or policy()
        super().__init__(filename, encoding="utf8")
        self.started = os.path.getmtime(filename)

    def emit(self, record):
        # Handler.handle holds the handler mutex across emit, including rollover.
        path = Path(self.baseFilename)
        size = self.stream.tell()
        age_limit = self.limits["log_days"] * 86400 / self.limits["log_segments"]
        size_limit = max(1, self.limits["log_bytes"] // self.limits["log_segments"])
        if size and (size >= size_limit or time.time() - self.started >= age_limit):
            self.stream.close()
            try:
                path.rename(path.with_name(f"vastar.log.{time.time_ns()}-{uuid.uuid4().hex}"))
                self.started = time.time()
            except OSError:
                pass  # Another handle may deny rename. Append this event and retry next time.
            finally:
                self.stream = self._open()
        super().emit(record)
