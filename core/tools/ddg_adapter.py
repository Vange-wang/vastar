"""DuckDuckGo HTML results; challenges and layout changes fail explicitly."""
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from core.harness.network_policy import NetworkError

VOID_TAGS = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}


class ResultParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.current, self.field = [], None, None
        self.depth, self.no_results = 0, False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get('class', '').split()
        self.no_results |= 'no-results' in classes
        if self.field and tag not in VOID_TAGS:
            self.depth += 1
        if tag == 'a' and 'result__a' in classes and attrs.get('href'):
            link = urljoin('https://html.duckduckgo.com', attrs.get('href', ''))
            parsed = urlsplit(link)
            if parsed.hostname in {'duckduckgo.com', 'html.duckduckgo.com'}:
                link = parse_qs(parsed.query).get('uddg', [link])[0]
            if urlsplit(link).scheme not in {'https', 'http'}:
                return
            self.current = {'title': '', 'url': link, 'snippet': ''}
            self.rows.append(self.current)
            self.field, self.depth = 'title', 1
        elif 'result__snippet' in classes and self.current is not None:
            self.field, self.depth = 'snippet', 1

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.field:
            self.depth -= 1
            if self.depth <= 0:
                self.field = None

    def handle_data(self, data):
        if self.field and self.current is not None:
            self.current[self.field] += data


class DuckDuckGoAdapter:
    version = 'duckduckgo-html-v1'
    verification = 'HTML search; challenge detection enabled'
    content_types = ('text/html',)
    supports_pages = False

    def search_url(self, query, page):
        if page != 1:
            raise NetworkError('pagination_unavailable', 'Use next_cursor for this result page; DDG provider paging is unavailable')
        return 'https://html.duckduckgo.com/html/?' + urlencode({'q': query})

    def search_results(self, body):
        text = body.decode('utf-8-sig', errors='strict')
        if 'challenge-form' in text or 'anomaly-modal' in text:
            raise NetworkError('search_challenge', 'DuckDuckGo requires human verification; no search results obtained')
        parser = ResultParser()
        parser.feed(text)
        if not parser.rows and not parser.no_results:
            raise NetworkError('invalid_response', 'DuckDuckGo response has no recognized search results')
        return [{**row, 'title': ' '.join(row['title'].split()),
                 'snippet': ' '.join(row['snippet'].split())} for row in parser.rows]
