"""Traceable search/read results, immutable source versions, bounded cache and cursors."""
import hashlib
import time
from collections import OrderedDict
from datetime import UTC, datetime

from core.harness.network_policy import NetworkError
from core.tools.pagination import decode_cursor, encode_cursor
from memory.repositories.tasks import encode


class SearchService:
    def __init__(self, network, artifacts, provider, extractor):
        self.network, self.artifacts, self.provider, self.extractor = network, artifacts, provider, extractor
        self.cache = OrderedDict()

    def source(self, url, cancel, usage, *, types, refresh):
        canonical, _, _ = self.network.policy.target(url)
        cached = self.cache.get(canonical)
        if cached and not refresh and time.time() - cached["cached_at"] < self.network.policy.cache_seconds:
            # Validate immutable artifact before trusting a cached reference.
            self.artifacts.get(cached["meta"].artifact_id, cached["meta"].version)
            return {**cached, "cache_hit": True}
        page = self.network.fetch(canonical, cancel, types=types, usage=usage)
        meta = self.artifacts.put(page["body"], source_ref="web:" + canonical)
        record = {**page, "meta": meta, "cached_at": time.time(), "cache_hit": False}
        self.cache[canonical] = record
        self.cache.move_to_end(canonical)
        while len(self.cache) > 16:
            self.cache.popitem(last=False)
        return record

    def _fields(self, source):
        return {"retrieved_at": datetime.fromtimestamp(source["retrieved_at"], UTC).isoformat(),
                "source_artifact": source["meta"].uri, "source_version": source["meta"].hash,
                "artifact_version": source["meta"].version, "content_type": source["content_type"],
                "cached_at": datetime.fromtimestamp(source["cached_at"], UTC).isoformat(),
                "cache_age_seconds": max(0, time.time() - source["cached_at"]), "cache_hit": source["cache_hit"],
                "adapter_version": self.provider.version, "trust": "external_evidence",
                "instructions_executable": False}

    def search(self, query, cancel, usage, *, page=1, cursor=None, page_size=5, refresh=False):
        if not isinstance(query, str) or not 1 <= len(query) <= 1024 or type(page) is not int or not 1 <= page <= 100:
            raise NetworkError("invalid_parameters", "query length 1..1024 and page 1..100 required")
        if type(page_size) is not int or not 1 <= page_size <= 10:
            raise NetworkError("invalid_parameters", "search page_size must be 1..10")
        url = self.provider.search_url(query, page)
        source = self.source(url, cancel, usage, types=("application/json",), refresh=refresh)
        results = self.provider.search_results(source["body"])
        offset = self._offset(cursor, url, source)
        if offset > len(results):
            raise NetworkError("invalid_cursor", "cursor outside source result set")
        rows = []
        for row in results[offset:offset + page_size]:
            # A result URL is evidence, not permission to fetch it. Reject secret-bearing/local links too.
            self.network.policy.target(row["url"])
            item = {**row, **self._fields(source), "snippet": row["snippet"][:1000],
                    "snippet_truncated": len(row["snippet"]) > 1000,
                    "title": row["title"][:200], "title_truncated": len(row["title"]) > 200}
            if len(encode([*rows, item]).encode()) > 48000:
                break
            rows.append(item)
        end = offset + len(rows)
        return {"status": "ok" if results else "empty_results", "results": rows,
                "next_cursor": self._cursor(url, source, end) if end < len(results) else None,
                "provider_page": page, "next_provider_page": page + 1 if results and page < 100 else None,
                "provider_has_more": "unknown", "source_artifact": source["meta"].uri}

    def read(self, url, cancel, usage, *, cursor=None, max_chars=8000, refresh=False):
        if type(max_chars) is not int or not 1 <= max_chars <= 8000:
            raise NetworkError("invalid_parameters", "max_chars must be 1..8000")
        source = self.source(url, cancel, usage, types=("text/html", "text/plain"), refresh=refresh)
        extracted = self.extractor.extract(source["body"], source["content_type"])
        offset = self._offset(cursor, url, source)
        text = extracted["text"]
        if offset > len(text):
            raise NetworkError("invalid_cursor", "cursor outside extracted text")
        end = min(offset + max_chars, len(text))
        result = {**self._fields(source), "title": extracted["title"][:200],
                "title_truncated": len(extracted["title"]) > 200, "url": source["url"],
                "snippet": text[:500], "content": text[offset:end], "total_chars": len(text),
                "status": "ok" if text else "empty_results", "truncated": end < len(text),
                "next_cursor": self._cursor(url, source, end) if end < len(text) else None}
        while len(encode(result).encode()) > 48000 and end > offset:
            end = offset + (end - offset) // 2
            result.update(content=text[offset:end], truncated=True, next_cursor=self._cursor(url, source, end))
        return result

    def _cursor(self, url, source, offset):
        return encode_cursor({"url_hash": hashlib.sha256(url.encode()).hexdigest(),
                              "source_version": source["meta"].hash, "offset": offset})

    def _offset(self, cursor, url, source):
        if cursor is None:
            return 0
        try:
            data = decode_cursor(cursor)
            if data["url_hash"] != hashlib.sha256(url.encode()).hexdigest() or data["source_version"] != source["meta"].hash:
                raise ValueError()
            if type(data["offset"]) is not int or data["offset"] < 0:
                raise ValueError()
            return data["offset"]
        except (ValueError, TypeError, KeyError):
            raise NetworkError("stale_cursor", "URL/source version changed; cursor invalid") from None
