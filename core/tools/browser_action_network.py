"""One exactly approved anonymous request; no redirects, retries, cookies or ambient auth."""
import base64
import hmac
import time

from core.harness.network_policy import NetworkError
from core.tools.browser_action_contract import input_value
from core.tools.browser_action_ledger import permit
from core.tools.browser_network import TYPES, BrowserBroker
from core.tools.browser_protocol import BrowserFailure
from core.tools.network_transport import checkpoint


class ActionToken:
    def __init__(self, authorized):
        self.authorized, self.control = authorized, None

    def is_set(self):
        # Once dispatch is permitted, cancellation cannot prove the action did not happen.
        return not self.authorized() and not (self.control and self.control.commit_started)


class ActionBroker(BrowserBroker):
    def __init__(self, network, config, token, args, control, authorized):
        super().__init__(network, config.get('readonly_urls', []), token, {})
        self.raw_network, self.config, self.args = network, config, args
        self.control, self.authorized, self.sent = control, authorized, False
        self.expected = args.get('request')
        self.body = None
        if self.expected:
            url = network.policy.target(self.expected['url'])[0]
            if url not in config.get('write_urls', []) or not callable(getattr(network.transport, 'send', None)):
                raise BrowserFailure('write_endpoint_unavailable')
            if network.transport.live:
                network.policy.require_live_grant()
            self.body = input_value(config, self.expected['body_ref'], args['origin']).encode('utf-8')

    def authorize_action(self):
        if not self.authorized():
            return {'approved': False}
        permit(self.control, self.authorized)
        return {'approved': True}

    def action_proof(self, proof):
        if proof.get('verified') is True and self.control.commit_started:
            self.control.facts['business_proof'] = {'verified': True, 'kind': proof['kind']}

    def __call__(self, request):
        if self.expected and request['url'] == self.expected['url']:
            try:
                return self._send(request)
            except Exception:
                return {'error': 'action_request_unknown' if self.sent else 'action_request_denied'}
        return super().__call__(request)

    def _send(self, request):
        expected = self.expected
        body = base64.b64decode(request.get('body', ''), validate=True)
        kind = request.get('content_type', '').split(';')[0]
        if (self.sent or not self.control.commit_started or not self.authorized()
                or request['method'] != expected['method'] or kind != expected['content_type']
                or not hmac.compare_digest(body, self.body)):
            raise NetworkError('request_denied', 'request differs from approval')
        service = self.raw_network
        url, host, port = service.policy.target(expected['url'])
        if service.transport.live:
            service.policy.require_live_grant()
        deadline = time.monotonic() + service.policy.timeout
        self.usage.setdefault('bytes', 0)
        self.usage.setdefault('requests', 0)
        self.network._robots(url, self.cancel, deadline, self.usage)
        address = service.policy.addresses(host, service.transport.resolve(host, port, self.cancel, deadline))[0]
        checkpoint(self.cancel, deadline)
        if self.usage['requests'] >= 16:
            raise NetworkError('budget_exceeded', 'physical request limit reached')
        self.usage['requests'] += 1
        self.sent = True  # Before the external call. Uncertain send is never repeated.
        response = service.transport.send(url, address, {'Content-Type': kind, 'Accept-Encoding': 'identity',
            'User-Agent': 'Vastar/1.0'}, self.cancel, deadline, method=expected['method'], body=body)
        try:
            if not 200 <= response.status < 300 or 'attachment' in response.headers.get('content-disposition', ''):
                raise NetworkError('action_response_unknown', 'no redirect or download follow-up')
            content_type = response.headers.get('content-type', '').split(';')[0]
            if content_type not in TYPES:
                raise NetworkError('content_type', 'unsupported response')
            data = service._body(response, self.cancel, deadline, self.usage)
            return {'status': response.status, 'content_type': content_type,
                    'body': base64.b64encode(data).decode()}
        finally:
            response.close()
