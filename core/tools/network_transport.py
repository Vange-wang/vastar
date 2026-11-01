"""Validated direct HTTP or explicitly selected trusted system proxy; TLS verifies host."""
import concurrent.futures
import http.client
import ipaddress
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit
from urllib.request import getproxies

from core.harness.network_policy import NetworkError


def checkpoint(cancel, deadline):
    if cancel.is_set():
        raise NetworkError("cancelled", "request cancelled; issued traffic cannot be undone")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise NetworkError("timeout", "network deadline exceeded")
    return remaining


class Response:
    def __init__(self, connection, response, stop, owned):
        self.connection, self.response = connection, response
        self.stop, self.owned = stop, owned
        self.status = response.status
        self.headers = {k.lower(): v for k, v in response.getheaders()}

    def read(self, count, timeout):
        self.owned["socket"].settimeout(timeout)
        return self.response.read1(count)

    def close(self):
        self.stop.set()
        self.response.close()
        self.connection.close()


class NativeHttpTransport:
    live = True

    def __init__(self, proxy_mode="direct"):
        self.proxy_mode = proxy_mode
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="vastar-dns")
        self.lock, self.pending = threading.Lock(), set()
        self.slots = threading.BoundedSemaphore(2)

    def resolve(self, host, port, cancel, deadline):
        if not self.slots.acquire(blocking=False):
            raise NetworkError("busy", "DNS resolver capacity reached")
        future = self.pool.submit(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
        with self.lock:
            self.pending.add(future)
        future.add_done_callback(self._resolved)
        while True:
            remaining = checkpoint(cancel, deadline)
            try:
                return list(dict.fromkeys(row[4][0] for row in future.result(timeout=min(.05, remaining))))
            except concurrent.futures.TimeoutError:
                continue

    def _resolved(self, future):
        with self.lock:
            self.pending.discard(future)
        self.slots.release()

    def open(self, url, address, headers, cancel, deadline):
        return self._open(url, address, headers, cancel, deadline)

    def send(self, url, address, headers, cancel, deadline, *, method, body):
        return self._open(url, address, headers, cancel, deadline, method=method, body=body)

    def _open(self, url, address, headers, cancel, deadline, *, method='GET', body=None):
        parsed = urlsplit(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        connection = http.client.HTTPConnection(parsed.hostname, port, timeout=checkpoint(cancel, deadline))
        stop = threading.Event()
        try:
            proxy = getproxies().get(parsed.scheme) if self.proxy_mode == "system" else None
            # A policy-approved literal loopback must reach this host, not the proxy host.
            if parsed.hostname == address and ipaddress.ip_address(address).is_loopback:
                proxy = None
            endpoint = urlsplit(proxy) if proxy else None
            if endpoint and (endpoint.scheme != "http" or endpoint.username or endpoint.password or not endpoint.hostname):
                raise NetworkError("proxy_unsupported", "Only an HTTP proxy without embedded credentials is supported")
            target = (endpoint.hostname, endpoint.port or 80) if endpoint else (address, port)
            sock = socket.create_connection(target, timeout=checkpoint(cancel, deadline))
            # System mode delegates final DNS routing to the explicitly trusted proxy.
            if endpoint:
                connection.set_tunnel(parsed.hostname, port)
            connection.sock = sock
            owned = {"socket": sock}
            watcher = threading.Thread(target=_interrupt, args=(owned, stop, cancel, deadline), daemon=True)
            watcher.start()
            if endpoint:
                connection._tunnel()
            if parsed.scheme == "https":
                sock.settimeout(checkpoint(cancel, deadline))
                connection.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname)
                owned["socket"] = connection.sock
            # HTTPConnection sees an existing socket and never resolves the hostname again.
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            if body is None:
                connection.request(method, path, headers=headers)
            else:
                connection.request(method, path, body=body, headers=headers)
            return Response(connection, connection.getresponse(), stop, owned)
        except BaseException:
            stop.set()
            connection.close()
            raise

    def assert_idle(self):
        with self.lock:
            if self.pending:
                raise RuntimeError("DNS resolution still in flight; wait before adapter rollback")

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=True)


def _interrupt(owned, stop, cancel, deadline):
    while not stop.wait(.02):
        if cancel.is_set() or time.monotonic() >= deadline:
            sock = owned["socket"]
            if sock:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            return
