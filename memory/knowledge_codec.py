"""S09 structured fact payload, stored through the existing MemoryService audit chain."""
import json

FORMAT = 'vastar.project-knowledge.v1'
STATES = {'effective', 'suggestion', 'pending', 'obsolete'}


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def decode(text):
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(value, dict) or value.get('format') != FORMAT:
        return None
    required = {'format', 'project_id', 'scope', 'key', 'state', 'conclusion', 'source', 'supersedes'}
    if set(value) != required or value['state'] not in STATES or not isinstance(value['source'], dict):
        raise ValueError('knowledge_payload_invalid')
    return value


def reference(project_id, item):
    return f"knowledge:{project_id}/{item['id']}@r{item['revision']}"


def verify_bound_source(memory, proposal):
    value = decode(proposal.get('text', ''))
    if value is None:
        return
    service = getattr(memory, 'knowledge', None)
    if service is None:
        raise ValueError('knowledge_dependency_missing：依赖缺失，无法核实归档来源')
    _, entry = service.sources.verify(value)
    from memory.policy import validate
    candidate = {k: proposal[k] for k in ('key', 'kind', 'text', 'validity') if k in proposal}
    expected = validate({**candidate, 'validity': entry.get('validity', {})})
    if candidate.get('kind') != 'fact' or validate(candidate) != expected:
        raise ValueError('knowledge_kind_or_validity_mismatch')
    if value['scope'] != proposal.get('scope', 'project:' + memory.project_id) or value['key'] != proposal['key']:
        raise ValueError('knowledge_scope_or_key_mismatch')
    if proposal.get('id') and service.stale(proposal):
        raise ValueError('knowledge_requires_revalidation')
