"""Anonymous B29 broker. Exact human-configured read URLs apply to every redirect."""
import base64
from urllib.parse import urlsplit

from core.harness.network_policy import NetworkError
from core.tools.network_service import NetworkService

TYPES = ('text/html', 'text/plain', 'text/css', 'application/javascript', 'text/javascript',
         'application/json', 'image/png', 'image/jpeg', 'image/gif', 'image/svg+xml',
         'image/webp', 'font/woff', 'font/woff2', 'application/font-woff')


class ReadonlyPolicy:
    def __init__(self, policy, urls):
        self.policy = policy
        self.urls = frozenset(policy.target(url)[0] for url in urls)
        self.robots = frozenset(f'{urlsplit(u).scheme}://{urlsplit(u).netloc}/robots.txt' for u in self.urls)

    def __getattr__(self, name):
        return getattr(self.policy, name)

    def target(self, url):
        result = self.policy.target(url)
        if result[0] not in self.urls | self.robots:
            raise NetworkError('navigation_not_authorized', 'URL lacks explicit read-only semantic authorization')
        return result


class AnonymousTransport:
    def __init__(self, transport):
        self.transport, self.live = transport, transport.live

    def resolve(self, *args):
        return self.transport.resolve(*args)

    def open(self, *args):
        response = self.transport.open(*args)
        if 'attachment' in response.headers.get('content-disposition', '').lower():
            response.close()
            raise NetworkError('download_blocked', 'download is outside readonly navigation')
        return response


class BrowserBroker:
    def __init__(self, network, urls, cancel, usage):
        policy = ReadonlyPolicy(network.policy, urls)
        self.network = NetworkService(policy, AnonymousTransport(network.transport))
        self.cancel, self.usage, self.cache = cancel, usage, {}

    def check(self, url):
        canonical = self.network.policy.target(url)[0]
        if canonical not in self.network.policy.urls:
            raise NetworkError('navigation_not_authorized', 'navigation URL has no readonly grant')
        if self.network.transport.live:
            self.network.policy.require_live_grant()
        return canonical

    def prepare_navigation(self, url):
        url = self.check(url)
        page = self.network.fetch(url, self.cancel, types=TYPES, usage=self.usage)
        self.cache[page['url']] = page
        return page['url']

    def __call__(self, request):
        try:
            if request.get('method') != 'GET':
                raise NetworkError('request_denied', 'only explicitly authorized readonly retrieval')
            url = self.check(request['url'])
            page = self.cache.pop(url, None) or self.network.fetch(url, self.cancel, types=TYPES, usage=self.usage)
            if page['url'] != url:
                self.cache[page['url']] = page
                return {'status': 302, 'location': page['url'], 'body': ''}
            return {'status': 200, 'body': base64.b64encode(page['body']).decode(),
                    'content_type': page['content_type']}
        except NetworkError as exc:
            return {'error': exc.code}
        except Exception:
            return {'error': 'network_unavailable'}
