"""Runtime-owned browser sessions; never reconnect by PID, cookie, or caller owner."""
import uuid
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from core.tools.browser_protocol import BrowserFailure


@dataclass
class BrowserSession:
    owner: dict
    connection: object
    browser_session_id: str = field(default_factory=lambda: 'browser-' + uuid.uuid4().hex)
    tabs: dict = field(default_factory=dict)
    tab_id: str = ''
    page_revision: str = ''
    origin: str = ''

    def require_owner(self, owner):
        if self.owner != owner:
            raise BrowserFailure('browser_scope_denied')

    def tab(self, tab_id):
        if tab_id not in self.tabs or self.tabs[tab_id].get('closed'):
            raise BrowserFailure('tab_closed')
        return self.tabs[tab_id]

    def update(self, result):
        self.tab_id, self.page_revision = result['tab_id'], result['page_revision']
        url = urlsplit(result['url'])
        self.origin = f'{url.scheme}://{url.netloc}'
        for tab in result.get('tabs', []):
            self.tabs.setdefault(tab['tab_id'], {}).update(closed=tab['closed'])
        self.tabs[result['tab_id']] = {'page_revision': result['page_revision'], 'closed': False,
                                       'origin': self.origin, 'url': result['url'],
                                       'targets': {e['target_id']: e for f in result.get('frames', [])
                                                   for e in f.get('elements', []) if e.get('target_id')}}


def validate_args(name, args):
    allowed = {'browser_session_id', 'tab_id', 'page_revision'}
    if name == 'browser_navigate':
        allowed.add('url')
        if not isinstance(args.get('url'), str) or not args['url'] or len(args['url']) > 4096:
            raise BrowserFailure('invalid_browser_parameters')
    elif name == 'browser_capabilities':
        allowed = set()
    elif not all(args.get(k) for k in ('browser_session_id', 'tab_id')):
        raise BrowserFailure('invalid_browser_parameters')
    if name == 'browser_screenshot' and not args.get('page_revision'):
        raise BrowserFailure('page_revision_required')
    if set(args) - allowed or any(not isinstance(v, str) for v in args.values()):
        raise BrowserFailure('invalid_browser_parameters')
    if args.get('tab_id') and not args.get('browser_session_id'):
        raise BrowserFailure('invalid_browser_parameters')
