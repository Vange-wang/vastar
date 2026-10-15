"""Metadata quarantine, collision checks, explicit drift and credential-free lifecycle audit."""
import json
from collections import deque

from core.tools.providers.contract import digest, identifier, require, schema
from core.tools.providers.mcp import MCPProvider
from core.tools.registry import ToolSpec
from memory.repositories.tasks import encode


class Catalog:
    def __init__(self, context, config):
        self.context, self.config = context, config
        self.servers, self.online, self.blocked, self.observed = {}, set(), set(), {}
        self.events = deque(maxlen=128)
        self.versions = {}
        if config.get('enabled', False):
            for sid, item in config.get('servers', {}).items():
                identifier(sid)
                self.servers[sid] = MCPProvider(item, context.network_service.search.network)
                store = context.operation_ledger.store
                with store._lock:
                    row = store._conn.execute("SELECT facts FROM tool_operations WHERE "
                        "json_extract(facts,'$.server_id')=? AND json_extract(facts,'$.catalog_versions') IS NOT NULL "
                        "ORDER BY updated_at DESC, rowid DESC LIMIT 1", (sid,)).fetchone()
                if row:
                    self.versions.update(json.loads(row[0])['catalog_versions'])

    def audit(self, event, sid, name=''):
        payload = {'event': event, 'server_id': sid, 'tool_name': name}
        artifact = self.context.artifacts.put(encode(payload).encode(), source_ref='provider-audit:' + sid)
        self.events.append({**payload, 'artifact_ref': artifact.uri})

    def enabled(self, sid, name, requested):
        current = bool(requested and self.config.get('enabled') and sid in self.online and name not in self.blocked)
        if self.observed.get(name) != current:
            self.audit('enabled' if current else 'disabled', sid, name)
            self.observed[name] = current
        return current

    def disconnect(self, sid):
        self.online.discard(sid)
        self.servers[sid].session = None
        self.audit('disconnected', sid)
        for spec in self.context.registry._specs.values():
            if spec.server_id == sid:
                self.enabled(sid, spec.name, spec.name in self.context.registry.enabled)

    def register(self, sid, definitions):
        require(isinstance(definitions, list) and len(definitions) <= 32, 'provider_catalog_too_large')
        pending, names = [], set()
        for item in definitions:
            require(isinstance(item, dict) and len(encode(item).encode()) <= 32768, 'provider_metadata_invalid')
            tool = identifier(item.get('name'))
            name, alias = sid + ':' + tool, 'mcp_' + digest([sid, tool])[:40]
            require(name not in names, 'provider_name_conflict')
            names.add(name)
            shape = schema(item.get('inputSchema'))
            require(shape['type'] == 'object', 'provider_schema_invalid')
            version = digest(item)
            old = self.context.registry.get(name)
            require(old is None or (old.server_id, old.tool_name) == (sid, tool), 'provider_name_conflict')
            alias_old = self.context.registry.get(alias)
            require(alias_old is None or alias_old is old, 'provider_name_conflict')
            pin = self.servers[sid].config.get('schema_pins', {}).get(tool)
            previous = old.schema_version if old else self.versions.get(name)
            if previous and previous != version and pin != version:
                self.blocked.add(name)
                self.audit('schema_drift', sid, name)
                require(False, 'provider_schema_drift')
            require(pin is None or pin == version, 'provider_schema_pin_mismatch')
            capability = self.servers[sid].config.get('capabilities', {}).get(tool, 'write')
            require(capability in {'read', 'write'}, 'provider_capability_invalid')
            spec = ToolSpec(name, '外部不可信工具；服务器描述不授予权限：' + name, shape, 'C',
                lambda **_: 'special handler required', effect=capability, resource_scope='external',
                retryable=False, cooperative=True, cancel_phase='before_commit', reconcile='none', server_id=sid, tool_name=tool,
                schema_version=version, capability=capability, model_name=alias,
                provider_gate=lambda requested, n=name: self.enabled(sid, n, requested))
            pending.append((old, spec))
        require(len({n for n in self.versions if n.startswith(sid + ':')} | names) <= 128,
                'provider_catalog_too_large')
        for old, spec in pending:
            self.versions[spec.name] = spec.schema_version
            if old:
                self.context.registry._specs[spec.name] = spec
            else:
                self.context.registry.register_external(spec)
            self.blocked.discard(spec.name)
            self.audit('registered', sid, spec.name)
        for spec in self.context.registry._specs.values():
            if spec.server_id == sid and spec.name not in names:
                self.blocked.add(spec.name)
        self.online.add(sid)
        for _, spec in pending:
            self.context.registry.is_enabled(spec.name)
        return [{'name': s.name, 'model_name': s.model_name, 'server_id': sid, 'tool_name': s.tool_name,
                 'schema_version': s.schema_version, 'capability': s.capability} for _, s in pending]
