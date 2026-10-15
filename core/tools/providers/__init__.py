"""Default-closed external registration; assembly never contacts a server."""
from core.tools.providers.catalog import Catalog
from core.tools.registry import ToolSpec


def install(context, raw):
    context.provider_service = Catalog(context, raw.get('providers', {}))
    context.registry.register(ToolSpec('provider_control',
        '查看、显式发现/重连或断开已配置外部服务器；新工具不自动启用；发现需人工确认。',
        {'type': 'object', 'properties': {'action': {'enum': ['status', 'discover', 'disconnect']},
         'server_id': {'type': 'string'}}, 'required': ['action', 'server_id'], 'additionalProperties': False},
        'C', lambda **_: 'special handler required', effect='write', resource_scope='external', retryable=False, cooperative=True))


async def dispatch(context, spec, args, session_id, run_id, operation_id, cancel, emitter):
    if spec.name != 'provider_control' and not spec.server_id:
        return None
    from core.tools.providers.execution import execute
    return await execute(context, spec, args, session_id, run_id, operation_id, cancel, emitter)
