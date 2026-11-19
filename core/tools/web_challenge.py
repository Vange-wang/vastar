"""Recognize DDG's interactive challenge, not words in an ordinary article."""
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from core.harness.network_policy import NetworkError

DDG_HOSTS = {'duckduckgo.com', 'html.duckduckgo.com', 'lite.duckduckgo.com'}


class _DDGChallenge(HTMLParser):
    def __init__(self, url):
        super().__init__(convert_charrefs=True)
        self.url, self.active, self.hidden = url, False, 0
        self.modal, self.checkbox, self.found = False, False, False

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'template', 'noscript'}:
            self.hidden += 1
        if self.hidden:
            return
        attrs = dict(attrs)
        if tag == 'form':
            try:
                target = urlsplit(urljoin(self.url, attrs.get('action') or ''))
            except ValueError:
                self.active = False
                return
            self.active = (attrs.get('id') == 'challenge-form' and
                           (attrs.get('method') or '').lower() == 'post' and
                           target.scheme in {'http', 'https'} and target.hostname in DDG_HOSTS and
                           target.path == '/anomaly.js')
            self.modal, self.checkbox = False, False
        if self.active:
            classes = (attrs.get('class') or '').split()
            self.modal |= 'anomaly-modal__modal' in classes
            self.checkbox |= (tag == 'input' and (attrs.get('type') or '').lower() == 'checkbox' and
                              (attrs.get('name') or '').startswith('image-check_'))
            self.found |= self.modal and self.checkbox

    def handle_endtag(self, tag):
        if tag in {'script', 'style', 'template', 'noscript'}:
            self.hidden = max(0, self.hidden - 1)
        if not self.hidden and tag == 'form':
            self.active = False


def reject_challenge(source):
    if source['content_type'] != 'text/html' or urlsplit(source['url']).hostname not in DDG_HOSTS:
        return
    parser = _DDGChallenge(source['url'])
    # Existing extractor owns strict encoding errors; this probe never promotes invalid text to content.
    parser.feed(source['body'].decode('utf-8-sig', errors='replace'))
    if parser.found:
        raise NetworkError('provider_challenge', 'DuckDuckGo requires human verification; page content withheld')
