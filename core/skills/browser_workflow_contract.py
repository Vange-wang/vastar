"""S07 definitions contain semantic selectors and local references, never input values."""
import re
from urllib.parse import urlsplit

from core.tools.browser_protocol import BrowserFailure


def require(condition, code='invalid_workflow'):
    if not condition:
        raise BrowserFailure(code)


def validate(args):
    require(isinstance(args, dict) and args.get('action') in {'save', 'advance', 'status'})
    common = {'action', 'workflow_id'}
    allowed = common | ({'definition'} if args['action'] == 'save' else
                        {'browser_session_id', 'tab_id'} if args['action'] == 'advance' else set())
    require(not set(args) - allowed)
    require(isinstance(args.get('workflow_id'), str) and re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', args['workflow_id']))
    if args['action'] == 'advance':
        require(all(isinstance(args.get(k), str) and args[k] for k in ('browser_session_id', 'tab_id')))
    if args['action'] == 'save':
        definition(args.get('definition'))


def definition(value):
    require(isinstance(value, dict) and value.get('mode') in {'collect', 'submit'})
    if value['mode'] == 'collect':
        require(set(value) == {'mode', 'urls', 'fields'})
        require(isinstance(value['urls'], list) and 1 <= len(value['urls']) <= 5)
        for url in value['urls']:
            address(url)
        fields = value['fields']
        require(isinstance(fields, list) and 1 <= len(fields) <= 8)
        for field in fields:
            require(isinstance(field, dict) and set(field) == {'name', 'tag'})
            identifier(field['name'])
            require(field['tag'] in {'title', 'h1', 'h2', 'h3', 'button', 'a'})
        require(len({f['name'] for f in fields}) == len(fields))
        return
    require(set(value) == {'mode', 'url', 'steps'})
    address(value['url'])
    require(isinstance(value['steps'], list) and 1 <= len(value['steps']) <= 8)
    for i, step in enumerate(value['steps']):
        require(isinstance(step, dict) and step.get('tool') in {'browser_fill', 'browser_click'})
        extra = {'value_ref'} if step['tool'] == 'browser_fill' else {'verify', 'request'}
        require(not set(step) - ({'tool', 'locator'} | extra))
        locator = step.get('locator', {})
        require(isinstance(locator, dict) and set(locator) == {'attribute', 'value'})
        require(locator['attribute'] in {'data-testid', 'aria-label', 'name'})
        identifier(locator['value'])
        if step['tool'] == 'browser_fill':
            require(step.get('value_ref') is None or isinstance(step['value_ref'], str))
            if step.get('value_ref'):
                identifier(step['value_ref'])
        else:
            require(i == len(value['steps']) - 1, 'submit_must_be_last')
            from core.tools.browser_action_contract import validate as validate_action
            validate_action('browser_click', {'browser_session_id': 'validate', 'tab_id': 'validate',
                'page_revision': '0' * 64, 'origin': 'https://invalid.example', 'target_id': 'validate',
                **{k: step[k] for k in ('verify', 'request') if k in step}})


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r'[a-zA-Z0-9_.:-]{1,128}', value))


def address(value):
    require(isinstance(value, str) and len(value) <= 4096)
    url = urlsplit(value)
    require(url.scheme in {'https', 'http'} and url.netloc and not url.username and not url.password)


def remedy(code):
    if code in {'target_not_observed', 'target_not_actionable', 'selector_missing'}:
        return 'selector_invalid: reread page; verify visible unique locator; manual review'
    if code in {'stale_page_revision', 'page_changed_during_capture', 'origin_changed', 'document_changed'}:
        return 'structure_changed: reread page; new preview and fresh approval; no replay'
    if code == 'field_renamed':
        return 'field_renamed: ask user to verify field mapping; never infer personal data'
    if code in {'authentication', 'access_restricted'}:
        return 'login_expired: user handles scoped login; no cookie import or cross-site reuse'
    return 'unreachable_or_unknown: inspect read-only receipt; manual handoff; no automatic retry'
