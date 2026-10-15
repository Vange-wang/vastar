"""Enforce URL/DNS/robots/redirect/size/type limits outside the replaceable adapter."""
import time
from urllib.parse import unquote, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from core.harness.network_policy import NetworkError
from core.tools.network_transport import NativeHttpTransport, checkpoint


class NetworkService:
    def __init__(self, policy, transport=None, credential=None, credential_host=None):
        self.policy, self.transport, self.credential = policy, transport or NativeHttpTransport(), credential
        self.credential_host = credential_host

    def fetch(self, url, cancel, *, types=("text/html", "text/plain"), usage=None):
        usage = usage if usage is not None else {}
        usage.setdefault("bytes", 0)
        usage.setdefault("requests", 0)
        deadline = time.monotonic() + min(self.policy.timeout, usage.get("time_budget_remaining_ms", 60000) / 1000)
        if self.transport.live:
            self.policy.require_live_grant()
        try:
            return self._follow(url, cancel, deadline, types, usage, robots=True)
        except NetworkError:
            raise
        except TimeoutError:
            raise NetworkError("timeout", "network deadline exceeded") from None
        except Exception:
            # Raw exceptions may contain URL query, headers or credentials; never surface them.
            checkpoint(cancel, deadline)
            raise NetworkError("offline", "transport unavailable; no content obtained") from None

    def _follow(self, url, cancel, deadline, types, usage, *, robots):
        for hop in range(self.policy.max_redirects + 1):
            checkpoint(cancel, deadline)
            url, host, port = self.policy.target(url)
            if robots:
                self._robots(url, cancel, deadline, usage)
            addresses = self.policy.addresses(host, self.transport.resolve(host, port, cancel, deadline))
            headers = {"User-Agent": "Vastar/1.0", "Accept-Encoding": "identity"}
            value = None
            if self.credential and host == self.credential_host and urlsplit(url).scheme == "https" and port == 443:
                # Lookup only after policy, scoped to exact origin; never forward auth across redirects.
                value = self.credential(host)
                if value:
                    if value in unquote(url) or "\r" in value or "\n" in value:
                        raise NetworkError("credential_url", "credential cannot occur in URL or contain controls")
                    headers["Authorization"] = "Bearer " + value
            if usage["requests"] >= 16:
                raise NetworkError("budget_exceeded", "physical request limit reached, including robots and redirects")
            usage["requests"] += 1
            response = self.transport.open(url, addresses[0], headers, cancel, deadline)
            try:
                checkpoint(cancel, deadline)
                if response.status in (301, 302, 303, 307, 308):
                    if hop == self.policy.max_redirects:
                        raise NetworkError("redirect_limit", "redirect limit reached")
                    url = urljoin(url, response.headers.get("location", ""))
                    continue
                self._status(response.status, robots)
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if content_type not in types:
                    raise NetworkError("content_type", "response content type not allowed")
                body = self._body(response, cancel, deadline, usage)
                if value and value.encode() in body:
                    raise NetworkError("credential_echo", "credential echoed in response; content withheld")
                return {"url": url, "body": body, "content_type": content_type,
                        "retrieved_at": time.time(), "redirects": hop}
            finally:
                response.close()
                headers.clear()
        raise NetworkError("redirect_limit", "redirect limit reached")

    def _body(self, response, cancel, deadline, usage):
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise NetworkError("content_type", "compressed responses not supported")
        if int(response.headers.get("content-length", "0")) > self.policy.max_bytes:
            raise NetworkError("response_too_large", "response exceeds byte limit; refused, no invented excerpt")
        chunks, size = [], 0
        while True:
            remaining = usage.get("byte_budget_remaining", 16 * self.policy.max_bytes) - usage["bytes"]
            if remaining <= 0:
                raise NetworkError("budget_exceeded", "network channel byte budget exhausted")
            data = response.read(min(16384, self.policy.max_bytes + 1 - size, remaining), checkpoint(cancel, deadline))
            checkpoint(cancel, deadline)
            if not data:
                return b"".join(chunks)
            size += len(data)
            usage["bytes"] += len(data)
            if size > self.policy.max_bytes:
                raise NetworkError("response_too_large", "stream exceeds byte limit; refused, no invented excerpt")
            chunks.append(data)

    def _robots(self, url, cancel, deadline, usage):
        origin = urlsplit(url)
        robots_url = f"{origin.scheme}://{origin.netloc}/robots.txt"
        try:
            page = self._follow(robots_url, cancel, deadline, ("text/plain",), usage, robots=False)
        except NetworkError as exc:
            if exc.code == "not_found":
                return
            if exc.code in {"authentication", "access_restricted"}:
                raise NetworkError("robots_denied", "robots access denied; page not fetched") from None
            raise
        parser = RobotFileParser()
        parser.parse(page["body"].decode("utf-8", errors="replace").splitlines())
        if not parser.can_fetch("Vastar/1.0", url):
            raise NetworkError("robots_denied", "robots excludes requested resource")

    @staticmethod
    def _status(status, robots):
        code = {429: "rate_limited", 401: "authentication", 403: "access_restricted", 404: "not_found"}.get(status)
        if code:
            raise NetworkError(code, f"HTTP {status}; no content obtained")
        if not 200 <= status < 300:
            raise NetworkError("http_error", f"HTTP {status}; no content obtained")
