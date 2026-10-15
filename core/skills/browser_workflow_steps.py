"""One bounded observation/action per advance; all effects use the ordinary executor."""
import json
import os
from contextvars import ContextVar

from core.harness.questions import QuestionService
from core.skills.browser_workflow_contract import require
from core.tools.browser_network import ReadonlyPolicy
from core.tools.browser_protocol import BrowserFailure

DELEGATE = ContextVar('browser_workflow_delegate', default=None)


async def invoke(context, name, args, session_id, run_id, cancel, emitter, oid=None):
    from core.tools.executor import ToolExecutor
    token = DELEGATE.set((id(context), name))
    try:
        return await ToolExecutor(context).execute(name, args, session_id=session_id, request_id=run_id,
            operation_id=oid, cancel_event=cancel, ask_emitter=emitter)
    finally:
        DELEGATE.reset(token)


def validate_urls(context, definition):
    config = context.browser_service.config
    policy = context.network_service.search.network.policy
    readonly = ReadonlyPolicy(policy, config.get('readonly_urls', []))
    urls = definition['urls'] if definition['mode'] == 'collect' else [definition['url']]
    for url in urls:
        require(readonly.target(url)[0] in readonly.urls, 'navigation_not_authorized')
    for step in definition.get('steps', []):
        if step.get('request'):
            require(policy.target(step['request']['url'])[0] in config.get('write_urls', []),
                    'write_endpoint_unavailable')


def clarify(context, definition, index, session_id):
    if definition['mode'] != 'submit':
        return None
    step = definition['steps'][index]
    refs = ([step.get('value_ref')] if step['tool'] == 'browser_fill' else [])
    if step.get('request'):
        refs.append(step['request']['body_ref'])
    bindings = context.browser_service.config.get('input_env', {})
    missing = [ref for ref in refs if not ref or not bindings.get(ref, {}).get('env')
               or os.environ.get(bindings[ref]['env']) is None]
    if not missing:
        return None
    task = context.task_service.current(session_id)
    return QuestionService(context.task_service).ask(session_id, question=
        '工作流缺少本地数据引用。请核对字段并配置本人确认的数据引用；不要在回答中粘贴密码。'
        '定义缺少 value_ref 时须另存新定义；不猜测个人资料。', options=[], required=True,
        expected_revision=task['current_plan_revision'])['question_id']


def payload(out):
    if out.status != 'executed':
        raise BrowserFailure(out.origin or 'workflow_observation_failed')
    return json.loads(out.result)


async def collect(context, definition, index, tab, session_id, run_id, cancel, emitter, oid):
    data = payload(await invoke(context, 'browser_navigate', {**tab, 'url': definition['urls'][index]},
                                session_id, run_id, cancel, emitter, oid))
    meta, _, _ = context.artifacts.page_by_ref(data['structure_artifact'])
    _, body = context.artifacts.get(meta['artifact_id'], meta['version'])
    frames = json.loads(body)['frames']
    values = {}
    for field in definition['fields']:
        texts = ([frame.get('title', '') for frame in frames] if field['tag'] == 'title' else
                 [e.get('text', '') for frame in frames for e in frame.get('elements', [])
                  if e.get('tag') == field['tag']])
        values[field['name']] = '\n'.join(dict.fromkeys(t for t in texts if t)).strip()[:256] or None
    return {'values': values, 'source': {key: data[key] for key in
        ('url', 'origin', 'page_revision', 'structure_artifact', 'screenshot_artifact')},
        'partial': True, 'scope': 'bounded visible snapshot; each field truncated to 256 characters'}


async def submit(context, definition, index, tab, session_id, run_id, cancel, emitter, oid):
    data = payload(await invoke(context, 'browser_state', tab, session_id, run_id, cancel, emitter))
    require(data['url'] == definition['url'], 'document_changed')
    step = definition['steps'][index]
    observed = context.browser_service.sessions[tab['browser_session_id']].tab(tab['tab_id'])
    matches = [t for t in observed.get('targets', {}).values()
               if all(t.get(k) == v for k, v in step['locator'].items())]
    require(len(matches) == 1, 'selector_missing')
    args = {k: data[k] for k in ('browser_session_id', 'tab_id', 'page_revision', 'origin')}
    args.update(target_id=matches[0]['target_id'])
    args.update({k: step[k] for k in ('value_ref', 'verify', 'request') if k in step})
    return await invoke(context, step['tool'], args, session_id, run_id, cancel, emitter, oid)
