"""Explicit local MCP process, scoped more narrowly than a Run: one approved operation."""
import threading

from core.tools.providers.contract import digest, require
from core.tools.providers.mcp import MCPProvider
from core.tools.providers.stdio_io import Channel


class StdioProvider(MCPProvider):
    def __init__(self, config, network):
        super().__init__(config, network)
        self.lock = threading.Lock()
        self.channel, self.catalog_digest = None, None
        self.last_process = {}

    def _open(self, cancel, run_id):
        require(self.config.get('enabled') is True, 'provider_disabled')
        require(self.lock.acquire(blocking=False), 'provider_stdio_busy')
        try:
            self.last_process = {}
            self.channel = Channel(self.config, cancel, run_id)
        except BaseException:
            self.lock.release()
            raise

    def _finish(self, verify=False):
        channel = self.channel
        try:
            if channel:
                channel.close(verify=verify)
        finally:
            if channel:
                self.last_process = channel.diagnostic()
            self.channel = None
            self.lock.release()

    def discover(self, cancel, usage):
        self._open(cancel, getattr(cancel, 'run_id', ''))
        success = False
        try:
            definitions = super().discover(cancel, usage)
            self.catalog_digest = digest(definitions)
            success = True
            return definitions
        finally:
            self._finish(verify=success)

    def call(self, name, args, operation_id, cancel, usage, issued):
        require(self.catalog_digest is not None, 'provider_not_initialized')
        self._open(cancel, getattr(cancel, 'run_id', ''))
        success = False
        try:
            definitions = super().discover(cancel, usage)
            require(digest(definitions) == self.catalog_digest, 'provider_schema_drift')
            result = super().call(name, args, operation_id, cancel, usage, issued)
            success = True
            return result
        finally:
            self._finish(verify=success)

    def rpc(self, method, params, cancel, usage, *, notification=False, issued=lambda: None):
        require(self.channel is not None, 'provider_not_initialized')
        return self.channel.rpc(method, params, usage, notification=notification, issued=issued)

    def close(self):
        super().close()
        self.catalog_digest = None
        if self.channel:
            self.channel.close()
