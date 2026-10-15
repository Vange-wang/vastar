"""Pinned optional Playwright/Chromium; all execution stays in B28's offline Job."""
import hashlib
import json
import shutil
from pathlib import Path

VERSION = '1.63.0'
REVISION = '1243'
ENGINE_VERSION = '153.0.8010.12'
LIMITS = {'memory_bytes': 1536 * 1024 * 1024, 'wall_seconds': 120,
          'output_bytes': 24 * 1024 * 1024, 'scratch_bytes': 128 * 1024 * 1024}


def capabilities(config):
    result = {'adapter': 'playwright-core', 'adapter_version': VERSION,
              'engine': 'chromium-headless-shell', 'engine_revision': REVISION,
              'engine_version': ENGINE_VERSION, 'status': 'unavailable',
              'reason_code': 'external_dependency_missing', 'fallback_used': False,
              'limits': LIMITS, 'login': 'anonymous isolated context; no imported credentials'}
    try:
        package = Path(config['playwright_package'])
        node = Path(config['node_executable'])
        engine = Path(config['chromium_directory'])
        metadata = json.loads((package / 'package.json').read_text(encoding='utf-8'))
        browsers = json.loads((package / 'browsers.json').read_text(encoding='utf-8'))['browsers']
        pin = next(b for b in browsers if b['name'] == 'chromium-headless-shell')
        if (metadata['version'], str(pin['revision']), pin['browserVersion']) != (VERSION, REVISION, ENGINE_VERSION):
            result['reason_code'] = 'adapter_version_mismatch'
        elif not node.is_file() or not (engine / 'chrome-headless-shell.exe').is_file():
            pass
        else:
            result.update(status='available', reason_code='requires_isolated_startup_probe')
    except (KeyError, ValueError, OSError, StopIteration):
        pass
    return result


class BrowserPayload:
    private_output = True
    browser_private = True

    def __init__(self, config):
        self.config = dict(config)
        self.manifest = {'kind': 'browser', 'capability': capabilities(config), 'limits': LIMITS,
                         'external_evidence': True, 'login': 'anonymous',
                         'worker_sha256': {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                           for name in ('browser_worker.cjs', 'browser_capture.cjs', 'browser_spawn.cjs',
                                                        'browser_action.cjs', 'browser_targets.cjs')}}
        self.script = '.\\node.exe --preserve-symlinks --preserve-symlinks-main .\\browser_worker.cjs; exit $LASTEXITCODE'
        self.approval_lines = ['隔离匿名浏览器；页面不能访问本机文件或直接联网。',
                               '仅私有缓存可写；新匿名会话，不导入 cookie/token/登录态。',
                               '网络由独立策略代取；导航限人工只读 URL，写入另逐次确认；120 秒后结束整个进程树。']

    def stage(self, profile):
        if capabilities(self.config)['status'] != 'available':
            raise RuntimeError('browser dependency unavailable; no fallback')
        package, engine = Path(self.config['playwright_package']), Path(self.config['chromium_directory'])
        shutil.copytree(package, profile.work / 'playwright', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(engine, profile.work / 'engine')
        shutil.copy2(self.config['node_executable'], profile.work / 'node.exe')
        for name in ('browser_worker.cjs', 'browser_capture.cjs', 'browser_spawn.cjs', 'browser_action.cjs', 'browser_targets.cjs'):
            shutil.copy2(Path(__file__).with_name(name), profile.work / name)
        (profile.root / 'Temp' / 'browser').mkdir()
        profile.browser_scratch = True
        profile.memory_bytes = LIMITS['memory_bytes']


class PlaywrightAdapter:
    def __init__(self, context, config):
        self.context, self.config = context, config

    def capabilities(self):
        return capabilities(self.config)

    async def start(self, spec, owner, session_id, run_id, cancel, emitter):
        from core.tools.browser_protocol import BrowserConnection
        from core.tools.process_tools import start_approved

        payload = BrowserPayload(self.config)
        result = await start_approved(self.context, spec, {'script': payload.script, 'idle_timeout': 120},
                                      owner, session_id, run_id, None, cancel, emitter, payload=payload)
        if result.status != 'executed':
            raise PermissionError('browser launch not approved or unavailable')
        return BrowserConnection(self.context.process_service, result.audit)
