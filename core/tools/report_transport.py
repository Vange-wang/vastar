"""Bounded private stdout: unvalidated report bytes must never become artifacts."""
import base64

from core.tools.process_output import OutputLimit


class ReportOutput:
    MAX_BYTES = 48 * 1024 * 1024

    def __init__(self):
        self.data = bytearray()
        self.stderr = bytearray()
        self.incomplete = False

    @property
    def total(self):
        return len(self.data)

    def append(self, stream, data):
        if len(self.data) + len(data) > self.MAX_BYTES:
            raise OutputLimit("report staging output quota")
        if stream == "stdout":
            self.data.extend(data)
        elif len(self.stderr) < 16384:
            self.stderr.extend(data[:16384 - len(self.stderr)])

    def snapshot(self):
        return {"private_staging": True, "chunks": [], "total_bytes": len(self.data),
                "encoding": "utf-8", "truncated": False, "incomplete": self.incomplete}

    def read(self, cursor=0, max_bytes=16384):
        end = min(cursor + max_bytes, len(self.data))
        return {"fragments": [{"stream": "stdout", "data_base64": base64.b64encode(self.data[cursor:end]).decode()}],
                "next_cursor": end, "total_bytes": len(self.data)}


def output_for(payload, artifacts, job_id, encoding):
    from core.tools.process_output import ProcessOutput

    if getattr(payload, 'browser_private', False):
        from core.tools.browser_protocol import BrowserOutput
        return BrowserOutput()
    return ReportOutput() if getattr(payload, "private_output", False) else ProcessOutput(artifacts, job_id, encoding)
