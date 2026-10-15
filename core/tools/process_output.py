"""Lossless byte cursors, bounded cache/manifest and fixed-size durable artifact chunks."""
import base64
from collections import OrderedDict


class OutputLimit(Exception):
    pass


class ProcessOutput:
    BUFFER_BYTES = 65536
    MAX_CHUNKS = 4096
    MAX_BYTES = 64 * 1024 * 1024

    def __init__(self, artifacts, job_id, encoding="utf-8", snapshot=None):
        self.artifacts, self.job_id, self.encoding = artifacts, job_id, encoding
        self.chunks = list((snapshot or {}).get("chunks", []))
        self.total = (snapshot or {}).get("total_bytes", 0)
        self.cache, self.cache_bytes = OrderedDict(), 0
        self.limit_reached = (snapshot or {}).get("limit_reached", False)
        self.incomplete = (snapshot or {}).get("incomplete", False)

    def append(self, stream, data):
        if len(data) > 16384:
            raise ValueError("pipe read exceeded bounded chunk size")
        if len(self.chunks) >= self.MAX_CHUNKS or self.total + len(data) > self.MAX_BYTES:
            self.limit_reached = True
            raise OutputLimit("output quota reached; tree stopped; unread pipe tail explicitly discarded")
        # Serialize version allocation with other publishers on the shared Store.
        with self.artifacts._db._lock:
            meta = self.artifacts.put(data, source_ref=f"process:{self.job_id}:{stream}:{self.total}")
        self.chunks.append({"offset": self.total, "size": len(data), "stream": stream,
                            "artifact_ref": meta.uri, "artifact_id": meta.artifact_id, "version": meta.version})
        self.cache[meta.uri] = data
        self.cache_bytes += len(data)
        while self.cache_bytes > self.BUFFER_BYTES:
            _, old = self.cache.popitem(last=False)
            self.cache_bytes -= len(old)
        self.total += len(data)

    def snapshot(self):
        return {"chunks": list(self.chunks), "total_bytes": self.total, "encoding": self.encoding,
                "truncated": self.total > self.BUFFER_BYTES or self.limit_reached or self.incomplete,
                "incomplete": self.incomplete,
                "limit_reached": self.limit_reached,
                "buffer_bytes": self.BUFFER_BYTES, "max_bytes": self.MAX_BYTES,
                "max_chunks": self.MAX_CHUNKS}

    def read(self, cursor=0, max_bytes=16384):
        if type(cursor) is not int or not 0 <= cursor <= self.total:
            raise ValueError("cursor outside captured byte range")
        if type(max_bytes) is not int or not 1 <= max_bytes <= 32768:
            raise ValueError("max_bytes must be in [1,32768]")
        fragments, end = [], min(cursor + max_bytes, self.total)
        for chunk in self.chunks:
            start, stop = chunk["offset"], chunk["offset"] + chunk["size"]
            if stop <= cursor or start >= end:
                continue
            data = self.cache.get(chunk["artifact_ref"])
            if data is None:
                _, data = self.artifacts.get(chunk["artifact_id"], chunk["version"])
            piece = data[max(cursor - start, 0):min(end - start, len(data))]
            fragments.append({**chunk, "offset": max(cursor, start), "size": len(piece),
                              "text": piece.decode(self.encoding, errors="replace")[:128],
                              "text_preview_limited": len(piece.decode(self.encoding, errors="replace")) > 128,
                              "data_base64": base64.b64encode(piece).decode("ascii")})
            if len(fragments) == 32:
                end = max(cursor, start) + len(piece)
                break
        return {"fragments": fragments, "next_cursor": end, "total_bytes": self.total,
                "truncated": self.total > self.BUFFER_BYTES or self.limit_reached or self.incomplete,
                "incomplete": self.incomplete,
                "limit_reached": self.limit_reached, "encoding": self.encoding,
                "decoding": "text preview may split characters; concatenate base64 bytes per stream then decode"}
