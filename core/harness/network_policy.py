"""One policy before DNS and every redirect; adapters cannot grant network authority."""
import ipaddress
import math
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit, urlunsplit


class NetworkError(Exception):
    def __init__(self, code, detail):
        self.code = code
        super().__init__(detail)


@dataclass(frozen=True)
class NetworkPolicy:
    enabled: bool = False
    allowed_hosts: tuple = ()
    private_hosts: tuple = ()
    allowed_ports: tuple = (443,)
    timeout: float = 10
    max_redirects: int = 3
    max_bytes: int = 1024 * 1024
    cache_seconds: int = 30
    proxy_mode: str = "direct"
    authorization: str = ""
    declared_cost: str = ""

    @classmethod
    def from_config(cls, config):
        raw = config.get("network", {})
        return cls(enabled=raw.get("enabled", False), allowed_hosts=tuple(raw.get("allowed_hosts", [])),
                   private_hosts=tuple(raw.get("private_hosts", [])),
                   allowed_ports=tuple(raw.get("allowed_ports", [443])),
                   timeout=raw.get("timeout", 10), max_redirects=raw.get("max_redirects", 3),
                   max_bytes=raw.get("max_bytes", 1048576), cache_seconds=raw.get("cache_seconds", 30),
                   proxy_mode=raw.get("proxy_mode", "direct"), authorization=raw.get("authorization", ""),
                   declared_cost=raw.get("declared_cost", ""))

    def __post_init__(self):
        if type(self.enabled) is not bool or self.proxy_mode != "direct":
            raise ValueError("network enabled must be boolean; only direct/no-proxy transport is supported")
        if type(self.timeout) not in (int, float) or not math.isfinite(self.timeout):
            raise ValueError("finite network timeout required")
        if any(type(v) is not int for v in (self.max_redirects, self.max_bytes, self.cache_seconds)):
            raise ValueError("network limits must be integers")
        if any(type(port) is not int or not 1 <= port <= 65535 for port in self.allowed_ports):
            raise ValueError("invalid allowed port")
        if not 0 < self.timeout <= 60 or not 0 <= self.max_redirects <= 5:
            raise ValueError("network timeout/redirect limit outside supported bounds")
        if not 1 <= self.max_bytes <= 2 * 1024 * 1024 or not 0 <= self.cache_seconds <= 300:
            raise ValueError("network response/cache limit outside supported bounds")
        if any(not isinstance(h, str) or '*' in h or h != h.lower() for h in self.allowed_hosts + self.private_hosts):
            raise ValueError("hosts must be explicit lowercase names; wildcards forbidden")
        if not set(self.private_hosts) <= set(self.allowed_hosts):
            raise ValueError("private exception must also be in allowed_hosts")

    def target(self, url):
        if not self.enabled:
            raise NetworkError("network_disabled", "C network is disabled")
        if not isinstance(url, str) or len(url) > 4096 or re.search(r"[\x00-\x20\\]", url):
            raise NetworkError("url_denied", "invalid URL")
        try:
            parsed = urlsplit(url)
            host = (parsed.hostname or "").encode("idna").decode("ascii").lower()
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except (ValueError, UnicodeError):
            raise NetworkError("url_denied", "invalid URL authority") from None
        if parsed.scheme not in {"http", "https"} or parsed.username is not None or parsed.password is not None:
            raise NetworkError("url_denied", "only HTTP(S) without embedded credentials is allowed")
        if host not in self.allowed_hosts or port not in self.allowed_ports:
            raise NetworkError("host_denied", "host/port is outside the explicit allowlist")
        if (host == "localhost" or host.endswith((".localhost", ".local", ".internal"))) and host not in self.private_hosts:
            raise NetworkError("private_address", "local host requires explicit private exception")
        if any(re.search(r"key|token|password|secret|credential|authorization|signature", k, re.I)
               for k, _ in parse_qsl(parsed.query, keep_blank_values=True)):
            raise NetworkError("credential_url", "credential-like query parameters are forbidden")
        canonical = urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))
        return canonical, host, port

    def addresses(self, host, addresses):
        if not addresses:
            raise NetworkError("offline", "DNS returned no usable address")
        for address in addresses:
            try:
                ip = ipaddress.ip_address(address)
            except ValueError:
                raise NetworkError("private_address", "invalid resolved address") from None
            internal = not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified or getattr(ip, "is_site_local", False)
            if internal and host not in self.private_hosts:
                raise NetworkError("private_address", "non-public DNS result requires explicit private exception")
        return addresses

    def require_live_grant(self):
        if not self.authorization or not self.declared_cost:
            raise NetworkError("live_not_authorized", "live request requires separately recorded human authorization and cost")
