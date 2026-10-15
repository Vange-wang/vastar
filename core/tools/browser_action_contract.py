"""No coordinates/JavaScript/plaintext input; targets must originate in the current snapshot."""
import os
import re
from urllib.parse import urlsplit

from core.tools.browser_protocol import BrowserFailure

WRITES = {'browser_click', 'browser_fill'}


def validate(name, args):
    common = {'browser_session_id', 'tab_id', 'page_revision', 'origin', 'target_id'}
    allowed = common | ({'value_ref'} if name == 'browser_fill' else {'verify', 'request'})
    if set(args) - allowed or not all(isinstance(args.get(k), str) and args[k] for k in common):
        raise BrowserFailure('invalid_browser_action')
    if not re.fullmatch('[a-f0-9]{64}', args['page_revision']):
        raise BrowserFailure('invalid_browser_action')
    if name == 'browser_fill' and not isinstance(args.get('value_ref'), str):
        raise BrowserFailure('input_reference_required')
    if name == 'browser_click':
        proof = args.get('verify', {})
        if set(proof) != {'testid', 'text'} or not all(isinstance(v, str) and 0 < len(v) <= 256 for v in proof.values()):
            raise BrowserFailure('business_verification_required')
    request = args.get('request')
    if request is not None:
        if (not isinstance(request, dict) or set(request) != {'url', 'method', 'body_ref', 'content_type'}
                or request['method'] not in {'POST', 'PUT', 'PATCH', 'DELETE', 'GET'}
                or request['content_type'] not in {'application/json', 'application/x-www-form-urlencoded', 'text/plain'}
                or not all(isinstance(v, str) for v in request.values())):
            raise BrowserFailure('invalid_action_request')


def target(session, args):
    tab = session.tab(args['tab_id'])
    if tab['page_revision'] != args['page_revision']:
        raise BrowserFailure('stale_page_revision')
    if tab['origin'] != args['origin']:
        raise BrowserFailure('origin_changed')
    entry = tab.get('targets', {}).get(args['target_id'])
    if entry is None:
        raise BrowserFailure('target_not_observed')
    frame = urlsplit(entry['frame_url'])
    if f'{frame.scheme}://{frame.netloc}' != args['origin']:
        raise BrowserFailure('target_origin_denied')
    return {**entry, 'document_url': tab['url']}


def input_value(config, ref, origin):
    binding = config.get('input_env', {}).get(ref, {})
    if binding.get('origin') != origin or not binding.get('env'):
        raise BrowserFailure('input_reference_denied')
    value = os.environ.get(binding['env'])
    if value is None or len(value.encode('utf-8')) > 8192:
        raise BrowserFailure('input_unavailable')
    return value
