"""Only validated anonymous snapshots become scoped, revision-checked artifacts."""
import base64
import json
import re
from urllib.parse import urlsplit

from core.tools.browser_protocol import BrowserFailure


def clean(value):
    if isinstance(value, str):
        value = re.sub(r'\b(?:Bearer\s+\S+|sk-[\w-]{8,}|eyJ[\w-]+\.[\w-]+\.[\w-]+)\b',
                       '[REDACTED]', value, flags=re.I)
        return re.sub(r'\b(cookie|token|password|passwd|secret|api[_-]?key|authorization)\s*[:=]\s*[^\s<]+',
                      r'\1=[REDACTED]', value, flags=re.I)
    if isinstance(value, list):
        return [clean(item) for item in value]
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items()}
    return value


def publish(store, session, result, broker):
    url = broker.check(result['url'])
    revision = result.get('page_revision', '')
    if not re.fullmatch('[a-f0-9]{64}', revision):
        raise BrowserFailure('invalid_page_revision')
    frames = result.get('frames')
    if not isinstance(frames, list) or not 1 <= len(frames) <= 16:
        raise BrowserFailure('invalid_page_structure')
    png = base64.b64decode(result['screenshot'], validate=True)
    if not png.startswith(b'\x89PNG\r\n\x1a\n') or len(png) > 4 * 1024 * 1024:
        raise BrowserFailure('invalid_screenshot')
    session.update(result)
    parsed = urlsplit(url)
    common = {'browser_session_id': session.browser_session_id, 'tab_id': result['tab_id'],
              'page_revision': revision, 'origin': f'{parsed.scheme}://{parsed.netloc}',
              'owner': session.owner, 'url': url, 'load_status': result['load_status'],
              'trust': 'external_evidence', 'instructions_executable': False,
              'partial': True, 'limitations': result.get('limitations', []),
              'events': result.get('events', []), 'tabs': result.get('tabs', [])}
    body = json.dumps(clean({**common, 'frames': frames}), ensure_ascii=False).encode('utf-8')
    if len(body) > 1024 * 1024:
        raise BrowserFailure('structure_too_large')
    source = f"browser:{session.browser_session_id}:{result['tab_id']}:{revision}"
    dom = store.put(body, source_ref=source)
    screenshot = store.put(png, source_ref=source)
    return {**clean(common), 'structure_artifact': dom.uri, 'screenshot_artifact': screenshot.uri,
            'frames_preview': clean([{'text': f['text'][:600]} for f in frames]),
            'screenshot_type': 'image/png', 'login_state': 'anonymous; credentials not imported'}
