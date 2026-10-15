"""One real wire adapter: SearXNG JSON search plus deterministic HTML/text extraction."""
import json
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import urlencode

from core.harness.network_policy import NetworkError


class SearchProvider(Protocol):
    def search_url(self, query: str, page: int) -> str: ...
    def search_results(self, body: bytes) -> list[dict]: ...


class Extractor(Protocol):
    def extract(self, body: bytes, content_type: str) -> dict: ...


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.title, self.hidden, self.in_title = [], [], 0, False

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "template"}:
            self.hidden += 1
        if tag == "title":
            self.in_title = True

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "template"}:
            self.hidden = max(0, self.hidden - 1)
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if not self.hidden and data.strip():
            (self.title if self.in_title else self.parts).append(data.strip())


class SearxngWebAdapter:
    version = "searxng-json-html-v1"
    verification = "configured; live_unverified"

    def __init__(self, endpoint):
        self.endpoint = endpoint.rstrip("/")

    def search_url(self, query, page):
        return self.endpoint + "/search?" + urlencode({"q": query, "format": "json", "pageno": page})

    def search_results(self, body):
        try:
            payload = json.loads(body)
            results = payload["results"]
            if not isinstance(results, list) or any(not isinstance(r, dict) for r in results):
                raise ValueError()
            if any(not isinstance(r.get("url"), str) or not isinstance(r.get("title", ""), str)
                   or not isinstance(r.get("content", ""), str) for r in results):
                raise ValueError()
            return [{"title": r.get("title", ""), "url": r["url"], "snippet": r.get("content", "")}
                    for r in results]
        except (ValueError, KeyError, TypeError):
            raise NetworkError("invalid_response", "search response is not valid provider JSON; no invented results") from None

    def extract(self, body, content_type):
        try:
            text = body.decode("utf-8-sig", errors="strict")
        except UnicodeError:
            raise NetworkError("unsupported_encoding", "only verified UTF-8 text decoding is supported") from None
        if content_type == "text/plain":
            return {"title": "", "text": text}
        parser = _Text()
        parser.feed(text)
        return {"title": " ".join(parser.title), "text": "\n".join(parser.parts)}
