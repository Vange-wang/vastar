"""类型、敏感性、范围、CAS 与覆盖策略；没有时间戳优先的隐式覆盖。"""
import re

from memory.retrieval import utc_time

KINDS = {'fact', 'preference', 'hypothesis', 'procedure'}
FIELDS = {'key', 'kind', 'text', 'source_refs', 'scope', 'validity', 'expected_revision', 'replace'}


def sensitive(text):
    return bool(re.search(r'(?i)(?:api[_ -]?key|password|passwd|secret|token|密码|口令|凭据|密钥)["\x27]?\s*(?:[=:：]|是|为)|'
                          r'"key"\s*:\s*"(?:api[_ -]?key|password|passwd|secret|token|密码|口令|凭据|密钥)"|'
                          r'\b(?:sk-|ghp_|github_pat_|AKIA)[A-Za-z0-9_-]{8,}|-----BEGIN .*PRIVATE KEY', text))


def validate(proposal):
    if not isinstance(proposal, dict) or set(proposal) - FIELDS:
        raise ValueError('unsupported_fields')
    if proposal.get('kind') not in KINDS:
        raise ValueError('invalid_kind')
    if any(not isinstance(proposal.get(k), str) or not proposal[k].strip() for k in ('key', 'text')):
        raise ValueError('key_and_text_required')
    if len(proposal['key']) > 120 or len(proposal['text']) > 4000:
        raise ValueError('memory_too_large')
    validity = proposal.get('validity', {})
    if not isinstance(validity, dict) or set(validity) - {'from', 'to'}:
        raise ValueError('invalid_validity')
    start, end = utc_time(validity.get('from')), utc_time(validity.get('to'))
    if dynamic(proposal) and not end:
        raise ValueError('dynamic_memory_requires_expiry')
    if start and end and start >= end:
        raise ValueError('invalid_validity')
    return {'from': start, 'to': end}


def active(item, now):
    v = item['validity']
    if dynamic(item) and not v['to']:
        return False  # 存量动态信息无到期时间只进重验清单，不猜永久有效期。
    return item['status'] == 'active' and (not v['from'] or v['from'] <= now) and (not v['to'] or now < v['to'])


def dynamic(item):
    return item['kind'] == 'procedure' or bool(re.search(r'(?i)(?:[a-z]:[/\\]|(?:^|[\s：])/[\w.]|'
                          r'(?:^|\s)(?:python|python3|git|npm|pip|pytest|pwsh)\s|执行|运行命令)', item['text']))


def scope_for(service, session_id, requested):
    project = 'project:' + service.project_id
    allowed = [project, 'user:' + service.user_id]
    task = service.store.task_for_session(session_id)
    if task:
        allowed.append('task:' + task['task_id'])
    if service.agent_id:
        allowed.append('agent:' + service.agent_id)
    scope = requested or project
    if scope not in allowed:
        raise ValueError('scope_denied')
    return scope


def decide_update(old, proposal, authority, now):
    expected = proposal.get('expected_revision')
    if type(expected) is not int or expected != (old['revision'] if old else 0):
        raise ValueError('expected_revision_conflict')
    if old is None:
        return 'new'
    if old['kind'] != proposal['kind']:
        raise ValueError('kind_cannot_replace')
    if old['status'] != 'active':
        raise ValueError('withdrawn_cannot_revive')
    if old['text'] == proposal['text'] and old['validity'] == proposal.get('validity', {'from': None, 'to': None}):
        return 'duplicate'
    if authority < old['authority']:
        raise ValueError('lower_authority')
    if not active({**old, 'validity': proposal['validity']}, now):
        raise ValueError('replacement_not_current')
    if proposal.get('replace') is not True:
        raise ValueError('conflict_requires_explicit_replace')
    return 'replace'
