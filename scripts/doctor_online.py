"""Opt-in bounded probes, isolated so stalled DNS cannot exceed the deadline."""
import json
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def probe(kind, config, server, timeout):
    try:
        result = subprocess.run([sys.executable, '-B', '-m', 'scripts.doctor_online'],
            input=json.dumps({'kind': kind, 'config': config, 'server': server, 'timeout': timeout}),
            capture_output=True, text=True, encoding='utf-8', timeout=timeout,
            cwd=Path(__file__).resolve().parents[1],
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            return {'error_type': 'probe_process_failed'}
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        return {'error_type': 'timeout'}
    except (OSError, ValueError):
        return {'error_type': 'probe_unavailable'}


def perform(kind, config, server, timeout):
    from core.harness.network_policy import NetworkPolicy
    from core.tools.network_service import NetworkService
    from core.tools.providers.mcp import VERSION, MCPProvider
    from core.tools.providers.protocol import negotiate
    network = None
    try:
        policy = replace(NetworkPolicy.from_config(config), timeout=timeout, max_redirects=0)
        network = NetworkService(policy)
        cancel = threading.Event()
        if kind == 'mcp':
            provider = MCPProvider({**server, 'timeout_seconds': timeout}, network)
            result = provider.rpc('initialize', {'protocolVersion': VERSION, 'capabilities': {},
                'clientInfo': {'name': 'Vastar-doctor', 'version': '1'}}, cancel, {})
            version = negotiate(result)
            provider.close()
            return {'result': 'initialize通过；未调用tools/list或tools/call', 'protocol_version': version}
        endpoint = config['network']['search_endpoint']
        url = urlsplit(endpoint)
        # /config is SearXNG's metadata endpoint; no search query or engine request.
        origin = urlunsplit((url.scheme, url.netloc, url.path.rsplit('/', 1)[0] + '/config', '', ''))
        reply = network.fetch(origin, cancel, types=('application/json',))
        data = json.loads(reply['body'])
        if not isinstance(data, dict) or not isinstance(data.get('engines'), list):
            return {'error_type': 'searxng_metadata_invalid'}
        return {'result': 'SearXNG配置端点可达；搜索结果质量未验证', 'engines': len(data['engines'])}
    except Exception as exc:
        # Never print URLs, payloads, authentication material or raw transport exception messages.
        return {'error_type': getattr(exc, 'code', type(exc).__name__)}
    finally:
        if network:
            network.transport.close()


def service_checks(config, make, online, timeout):
    network = config.get('network', {})
    providers = config.get('providers', {})
    servers = providers.get('servers', {})
    rows = []
    targets = [('search', None)]
    if not isinstance(servers, dict):
        rows.append(make('mcp', 'blocked', 'providers.servers必须为对象',
                         remediation='修正 providers.servers 为按服务器名索引的配置对象。'))
        servers = {}
    if not servers:
        rows.append(make('mcp', 'warning', '未验证：未配置MCP端点',
                         remediation='配置 providers.servers；获授权后使用 --online 验证initialize。'))
    targets += [('mcp:' + name, server) for name, server in servers.items()]
    for key, server in targets:
        kind = 'search' if key == 'search' else 'mcp'
        reason = None
        if network.get('enabled') is not True:
            reason = 'network.enabled关闭；未探网'
        elif kind == 'mcp' and providers.get('enabled') is not True:
            reason = 'providers.enabled关闭；未探网'
        elif kind == 'search' and (network.get('search_provider') != 'searxng' or not network.get('search_endpoint')):
            reason = '未配置SearXNG；未验证'
        elif kind == 'mcp' and (not isinstance(server, dict) or not server.get('url')):
            reason = 'MCP端点未配置；未验证'
        elif not online:
            reason = '未验证：离线模式不探测服务（包括localhost）'
        if reason:
            rows.append(make(key, 'warning', reason, remediation='核对 network / providers 配置；显式 --online 探活。'))
            continue
        outcome = probe(kind, config, server, timeout)
        error = outcome.get('error_type')
        rows.append(make(key, 'warning' if error else 'ok',
            f'探测失败：{error}' if error else outcome['result'], details=outcome,
            remediation='核对服务进程、端点、network授权/允许列表和凭据环境变量；doctor不启动服务。' if error else ''))
    return rows


if __name__ == '__main__':
    args = json.loads(sys.stdin.read())
    print(json.dumps(perform(**args)))
