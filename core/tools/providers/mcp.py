"""Pinned MCP Streamable HTTP; no subprocess, redirects, ambient auth or automatic resend."""
import json
import os
import time
import uuid
from urllib.parse import unquote, urlsplit

from core.tools.network_transport import checkpoint
from core.tools.providers.contract import ProviderError, require

VERSION = '2025-06-18'


class ToolProvider:
    """Transport contract implemented by MCP; caller owns authorization and durable intent."""
    def discover(self, cancel, usage):
        raise NotImplementedError

    def call(self, name, args, operation_id, cancel, usage, issued):
        raise NotImplementedError


class MCPProvider(ToolProvider):
    def __init__(self, config, network):
        self.config, self.network = dict(config), network
        self.session = None

    def discover(self, cancel, usage):
        self.session = None
        result = self.rpc('initialize', {'protocolVersion': VERSION, 'capabilities': {},
            'clientInfo': {'name': 'Vastar', 'version': '1.0'}}, cancel, usage)
        require(result.get('protocolVersion') == VERSION and isinstance(result.get('capabilities', {}).get('tools'), dict),
                'provider_protocol_unsupported')
        self.rpc('notifications/initialized', {}, cancel, usage, notification=True)
        result = self.rpc('tools/list', {}, cancel, usage)
        require(isinstance(result.get('tools'), list) and not result.get('nextCursor'), 'provider_listing_incomplete')
        return result['tools']

    def call(self, name, args, operation_id, cancel, usage, issued):
        return self.rpc('tools/call', {'name': name, 'arguments': args,
            '_meta': {'vastar/operation_id': operation_id}}, cancel, usage, issued=issued)

    def rpc(self, method, params, cancel, usage, *, notification=False, issued=lambda: None):
        policy, transport = self.network.policy, self.network.transport
        url, host, port = policy.target(self.config['url'])
        require(url == self.config['url'], 'provider_exact_url_required')
        if transport.live:
            policy.require_live_grant()
        deadline = time.monotonic() + min(policy.timeout, self.config.get('timeout_seconds', 20))
        self.network._robots(url, cancel, deadline, usage)
        address = policy.addresses(host, transport.resolve(host, port, cancel, deadline))[0]
        require(usage.get('requests', 0) < 16, 'provider_budget_exceeded')
        headers, credential = self.headers(url)
        rid = uuid.uuid4().hex
        message = {'jsonrpc': '2.0', 'method': method, 'params': params}
        if not notification:
            message['id'] = rid
        response = None
        try:
            checkpoint(cancel, deadline)
            usage['requests'] = usage.get('requests', 0) + 1
            issued()  # durable uncertainty boundary BEFORE handing bytes to transport
            response = transport.send(url, address, headers, cancel, deadline,
                                      method='POST', body=json.dumps(message).encode())
            require(200 <= response.status < 300, 'provider_disconnected')
            token = response.headers.get('mcp-session-id')
            if token:
                require(method == 'initialize' and len(token) <= 256 and all(33 <= ord(c) <= 126 for c in token),
                        'provider_invalid_session')
                self.session = token
            if notification:
                require(response.status == 202, 'provider_invalid_notification_reply')
                return {}
            result = read_reply(response, rid, self.network, cancel, deadline, usage)
            text = json.dumps(result, ensure_ascii=False, allow_nan=False)
            for secret in (credential, self.session):
                if secret:
                    text = text.replace(json.dumps(secret, ensure_ascii=False)[1:-1], '[REDACTED]')
            return json.loads(text)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(getattr(exc, 'code', 'provider_disconnected')) from None
        finally:
            headers.clear()
            if response:
                response.close()

    def headers(self, url):
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
                   'MCP-Protocol-Version': VERSION, 'Accept-Encoding': 'identity'}
        secret = None
        if self.config.get('credential_env'):
            require(urlsplit(url).scheme == 'https', 'provider_auth_requires_https')
            secret = os.environ.get(self.config['credential_env'])
            require(secret and len(secret) <= 8192 and all(32 <= ord(c) <= 126 for c in secret), 'provider_credential_unavailable')
            require(secret not in unquote(url), 'provider_credential_url')
            headers['Authorization'] = 'Bearer ' + secret
        if self.session:
            headers['Mcp-Session-Id'] = self.session
        return headers, secret


def read_reply(response, rid, network, cancel, deadline, usage):
    kind = response.headers.get('content-type', '').split(';')[0].strip().lower()
    require(kind in {'application/json', 'text/event-stream'}, 'provider_content_type')
    body = b''
    while True:
        chunk = response.read(16384, checkpoint(cancel, deadline))
        body += chunk
        usage['bytes'] = usage.get('bytes', 0) + len(chunk)
        require(usage['bytes'] <= network.policy.max_bytes, 'provider_result_too_large')
        if kind == 'text/event-stream':
            blocks = body.replace(b'\r\n', b'\n').split(b'\n\n')
            for block in blocks[:-1]:
                data = b'\n'.join(line[5:].lstrip() for line in block.splitlines() if line.startswith(b'data:'))
                if data:
                    return envelope(json.loads(data), rid)
            body = blocks[-1]
        if not chunk:
            require(kind == 'application/json', 'provider_disconnected')
            return envelope(json.loads(body), rid)


def envelope(value, rid):
    require(isinstance(value, dict) and value.get('jsonrpc') == '2.0' and value.get('id') == rid,
            'provider_unexpected_message')
    require('error' not in value and isinstance(value.get('result'), dict), 'provider_remote_error')
    return value['result']
