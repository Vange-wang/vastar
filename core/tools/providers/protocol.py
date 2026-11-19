"""Bounded handshake-era MCP versions and strict JSON-RPC response envelopes."""
import json

from core.tools.providers.contract import require

VERSIONS = ('2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05')


def negotiate(result, preferred=VERSIONS):
    version = result.get('protocolVersion')
    capabilities = result.get('capabilities')
    require(version in preferred and isinstance(capabilities, dict) and isinstance(capabilities.get('tools'), dict),
            'provider_protocol_unsupported')
    return version


def envelope(value, rid):
    require(isinstance(value, dict) and value.get('jsonrpc') == '2.0' and value.get('id') == rid,
            'provider_unexpected_message')
    require('error' not in value and isinstance(value.get('result'), dict), 'provider_remote_error')
    return value['result']


def decode(body, kind, rid):
    if kind == 'application/json':
        return envelope(json.loads(body), rid)
    messages = []
    for block in body.replace(b'\r\n', b'\n').split(b'\n\n'):
        data = b'\n'.join(line[5:].lstrip() for line in block.splitlines() if line.startswith(b'data:'))
        if data:
            messages.append(json.loads(data))
    require(len(messages) == 1, 'provider_duplicate_or_missing_reply')
    return envelope(messages[0], rid)
