"""S06 serial scope, delegated edits and bounded approved ProcessService validation."""
import asyncio
import contextvars
import json
import threading
from pathlib import Path

from core.skills import code_change_state as state
from core.skills.code_change_snapshot import OMIT, CodePayload, files, require, revision_facts
from core.tools.operation_receipt import ToolOutcome
from core.tools.permissions import is_env_path
from core.tools.registry import ToolSpec
from memory.repositories.tasks import encode

NAME, SKILL = 'code_change', 'code-change-validation'
DELEGATE = contextvars.ContextVar('code_change_delegate', default=None)
LOCK, BUSY = threading.Lock(), set()


def active(context):
    return context.skills is not None and SKILL in context.skills.available() and SKILL in context.skills.active_names()


def guard(context, spec):
    if active(context) and DELEGATE.get() != (id(context), spec.name) and spec.name != NAME and (
            spec.effect != 'read' or spec.name == 'validate_task_step') and spec.name not in {'terminate_process', 'ask_task_question'}:
        return ToolOutcome(spec.name, 'rejected', 'S06 requires a locked scope and serial delegated operation', origin='code_scope_gate')
    return None


def install(context):
    context.registry.register(ToolSpec(NAME, '锁定代码范围、委托逐文件修改、启动验证、查看真实回执；默认关闭。',
        {'type': 'object', 'properties': {'action': {'enum': ['lock', 'edit', 'verify', 'report']},
         'change_id': {'type': 'string'}, 'definition': {'type': 'object'}, 'tool': {'type': 'string'},
         'arguments': {'type': 'object'}, 'check': {'type': 'integer'}},
         'required': ['action', 'change_id'], 'additionalProperties': False},
        'C', lambda **_: 'special handler required', effect='write', retryable=False))


async def execute(context, args, session, run, cancel, emitter):
    key, held, row = None, False, None
    try:
        require(active(context), 'code_skill_disabled')
        require(set(args) <= {'action', 'change_id', 'definition', 'tool', 'arguments', 'check'}, 'code_arguments_invalid')
        require(isinstance(args.get('change_id'), str) and 0 < len(args['change_id']) <= 64, 'code_identity_invalid')
        context.process_service.require_task(context.task_service.current(session)['task_id'], run)
        if args['action'] == 'lock':
            key = str(Path(args['definition']['workspace']).resolve())
        else:
            row = state.load(context, session, args['change_id'])
            key = row['facts']['definition']['workspace']
        with LOCK:
            require(key not in BUSY, 'code_serial_busy')
            BUSY.add(key)
            held = True
        if args['action'] == 'lock':
            result = await lock_scope(context, args, session, run, cancel, emitter)
        else:
            if args['action'] == 'edit':
                result = await edit(context, row, args, session, run, cancel, emitter)
            elif args['action'] == 'verify':
                result = await verify(context, row, args, session, run, cancel, emitter)
            else:
                require(args['action'] == 'report', 'code_action_invalid')
                result = report(context, row)
        text = encode(result)
        if len(text.encode()) > 48000:
            artifact = context.artifacts.put(text.encode(), source_ref='code-change-output')
            text = encode({'artifact_ref': artifact.uri, 'pagination': 'read_artifact', 'completed': False})
        return ToolOutcome(NAME, 'executed', result=text, redact_result_preview=True)
    except Exception as exc:
        return ToolOutcome(NAME, 'rejected', str(exc) if isinstance(exc, ValueError) else type(exc).__name__,
                           origin='code_incomplete', redact_result_preview=True)
    finally:
        if held:
            with LOCK:
                BUSY.discard(key)


async def launch(context, payload, session, run, cancel, emitter, oid):
    from core.tools.process_tools import start_approved
    require(context.registry.is_enabled('start_process'), 'code_process_disabled')
    result = await start_approved(context, context.registry.get('start_process'), {'script': payload.script},
        context.task_service.current(session)['task_id'], session, run, oid, SkillCancel(context, cancel), emitter, payload=payload)
    require(result.status == 'executed', 'code_process_not_started')
    return json.loads(result.result)


async def lock_scope(context, args, session, run, cancel, emitter):
    definition = args['definition']
    require(set(definition) == {'workspace', 'scope', 'acceptance', 'checks'}, 'code_definition_invalid')
    scope, checks = definition['scope'], definition['checks']
    require(isinstance(scope, list) and 0 < len(scope) <= 32 and len(set(scope)) == len(scope), 'code_scope_invalid')
    require(isinstance(definition['acceptance'], str) and 0 < len(definition['acceptance']) <= 2048, 'code_acceptance_required')
    require(isinstance(checks, list) and 0 < len(checks) <= 4, 'code_checks_required')
    require(Path(definition['workspace']).is_absolute(), 'code_explicit_workspace_required')
    for check in checks:
        require(set(check) == {'command', 'rationale'} and all(isinstance(v, str) and 0 < len(v) <= 8192 for v in check.values()),
                'code_check_invalid')
    payload = CodePayload(definition['workspace'])
    spelling = {p.casefold(): p for p in payload.hashes}
    for rel in scope:
        require(isinstance(rel, str) and rel and '\\' not in rel and not Path(rel).is_absolute()
                and '..' not in Path(rel).parts and not any(c in rel for c in '*?:') and not rel.startswith('.git'), 'code_path_invalid')
        require(rel == Path(rel).as_posix() and spelling.get(rel.casefold(), rel) == rel, 'code_canonical_path_required')
        require(not any(p in OMIT or p.startswith('.env') or is_env_path(Path(p)) for p in Path(rel).parts)
                and Path(rel).suffix.lower() not in {'.db', '.sqlite', '.sqlite3'}, 'code_runtime_target_refused')
        require((payload.root / rel).resolve().is_relative_to(payload.root), 'code_path_escape')
        require(not any(rel == p.rstrip('/') or rel.startswith(p) for p in payload.omitted), 'code_runtime_target_refused')
        require(not (payload.root / rel).is_dir(), 'code_directory_scope_refused')
    job = await launch(context, payload, session, run, cancel, emitter, None)
    entry = context.process_service.lookup(job['job_id'], job['owner'], job['started_at'])
    await asyncio.to_thread(entry['done'].wait)
    info, text, output = state.output(context, job, job['owner'])
    require(state.passed(info), 'code_baseline_probe_failed')
    payload.verify_unchanged()
    baseline = state.probe(text)
    dirty = dirty_names(baseline['dirty_raw'])
    require(not any(p.casefold() == d.casefold().rstrip('/') or (d.endswith('/') and p.casefold().startswith(d.casefold()))
                    for p in scope for d in dirty), 'code_target_has_user_changes')
    originals = {}
    for rel in scope:
        if rel in payload.hashes:
            meta = context.artifacts.put((payload.root / rel).read_bytes(), source_ref='code-before:' + rel)
            originals[rel] = [meta.artifact_id, meta.version]
    baseline.update(job=job, output=output, dirty=sorted(dirty), omitted=payload.omitted,
                    revision=payload.revision, originals=originals, git_config=payload.git_config_hashes)
    definition = {**definition, 'workspace': str(payload.root)}
    row = state.save(context, session, args['change_id'], definition, baseline, payload.hashes, run)
    return {'locked': True, 'base_revision': baseline['base_revision'], 'dirty': sorted(dirty), 'scope': scope,
            'acceptance': definition['acceptance'], 'operation_id': row['operation_id'], 'workspace': str(payload.root)}


def dirty_names(raw):
    records, result, i = raw.split('\0'), set(), 0
    while i < len(records):
        item = records[i]
        if item:
            result.add(item[3:])
            if 'R' in item[:2] or 'C' in item[:2]:
                i += 1
                result.add(records[i])
        i += 1
    return result


def unchanged(row):
    from core.skills.code_change_git import configuration
    from core.skills.code_change_snapshot import gitdir
    facts = row['facts']
    current, omitted = files(facts['definition']['workspace'])
    require(revision_facts(facts['definition']['workspace']) == facts['baseline']['revision'], 'code_revision_changed')
    own, common = gitdir(Path(facts['definition']['workspace']))
    require(configuration(common, own)[1] == facts['baseline']['git_config'], 'code_git_config_changed')
    scope, before = set(facts['definition']['scope']), facts['hashes']
    require(omitted == facts['baseline']['omitted'], 'code_omissions_drift')
    require(all(current.get(p) == before.get(p) for p in set(current) | set(before) if p not in scope), 'code_unowned_or_config_drift')
    expected = facts.get('expected', before)
    require(all(current.get(p) == expected.get(p) for p in scope), 'code_concurrent_target_change')
    return current


def refresh(context, row):
    for attempt in row['facts']['attempts']:
        require('job' in attempt, 'code_incomplete_attempt_requires_review')
        if 'info' in attempt:
            continue
        info, text, output = state.output(context, attempt['job'], attempt['job']['owner'])
        attempt.update(info=info, output=output)
        if text:
            try:
                attempt['probe'] = state.probe(text)
            except (ValueError, IndexError):
                attempt['probe_error'] = True
    state.persist(context, row)


async def edit(context, row, args, session, run, cancel, emitter):
    from core.tools.executor import ToolExecutor
    refresh(context, row)
    unchanged(row)
    row['facts'].setdefault('expected', dict(row['facts']['hashes']))
    require(args['tool'] in {'write_file', 'edit_file', 'move_file', 'delete_path'}, 'code_edit_tool_invalid')
    values, definition = dict(args['arguments']), row['facts']['definition']
    for key in ('path', 'dest'):
        if key in values:
            require(values[key] in definition['scope'], 'code_edit_out_of_scope')
            values[key] = str(Path(definition['workspace']) / values[key])
    require('path' in values, 'code_edit_path_required')
    token = DELEGATE.set((id(context), args['tool']))
    try:
        outcome = await ToolExecutor(context).execute(args['tool'], values, session_id=session, request_id=run,
                                                      cancel_event=cancel, ask_emitter=emitter)
    finally:
        DELEGATE.reset(token)
    row['facts']['edits'].append({'tool': args['tool'], 'status': outcome.status, 'audit': outcome.audit})
    state.persist(context, row)
    require(outcome.status not in {'unknown', 'cancelled'}, 'code_edit_requires_reconciliation')
    expected = dict(row['facts']['expected'])
    if outcome.status == 'executed':
        expected = receipt_expected(outcome, values, definition, expected)
    unchanged({**row, 'facts': {**row['facts'], 'expected': expected}})
    row['facts']['expected'] = expected
    state.persist(context, row)
    return {'edit_status': outcome.status, 'reason': outcome.reason, 'completed': False}


def receipt_expected(outcome, values, definition, expected):
    """Acknowledge only committed target facts, never a post-await workspace snapshot."""
    audit, code = outcome.audit, 'code_edit_requires_reconciliation'
    require(audit.get('operation_state') == 'committed' and audit.get('active') is False
            and audit.get('verification') == 'completed', code)
    targets = {str(Path(values[k])): Path(values[k]).relative_to(definition['workspace']).as_posix()
               for k in ('path', 'dest') if k in values}
    before, after = audit.get('before', {}), audit.get('after', {})
    for path, rel in targets.items():
        old = {'exists': True, 'sha256': expected[rel]} if rel in expected else {'exists': False}
        require(before.get(path) == old, 'code_concurrent_target_change')
    if outcome.name in {'write_file', 'edit_file'}:
        proof = outcome.contract.get('write_proof', {})
        digest = proof.get('expected_hash')
        require(proof.get('match') is True and isinstance(digest, str) and len(digest) == 64
                and proof.get('actual_hash') == digest, code)
        changes = {values['path']: {'exists': True, 'sha256': digest}}
    elif outcome.name == 'move_file':
        require('dest' in values and before.get(values['path'], {}).get('sha256'), code)
        changes = {values['path']: {'exists': False}, values['dest']: before[values['path']]}
        require(audit.get('expected') == changes, code)
    else:
        require(outcome.name == 'delete_path' and outcome.contract.get('recovery'), code)
        changes = {values['path']: {'exists': False}}
    require(set(changes) == set(targets) and all(after.get(p) == fact for p, fact in changes.items()), code)
    for path, fact in changes.items():
        rel = targets[path]
        if fact['exists']:
            expected[rel] = fact['sha256']
        else:
            expected.pop(rel, None)
    return expected


async def verify(context, row, args, session, run, cancel, emitter):
    refresh(context, row)
    hashes = unchanged(row)
    index, facts = args['check'], row['facts']
    require(type(index) is int and 0 <= index < len(facts['definition']['checks']), 'code_check_invalid')
    require(len(facts['attempts']) < 12, 'code_retry_limit')
    baseline = {k: facts['baseline'][k] for k in ('base_revision', 'dirty_raw', 'diff')}
    payload = CodePayload(facts['definition']['workspace'], facts['definition']['checks'][index]['command'], baseline)
    attempt = {'check': index, 'hashes': hashes, 'phase': 'baseline' if hashes == facts['hashes'] else 'candidate'}
    facts['attempts'].append(attempt)
    state.persist(context, row)
    attempt['job'] = await launch(context, payload, session, run, cancel, emitter, row['operation_id'] + ':' + str(len(facts['attempts'])))
    state.persist(context, row)
    return {'job': attempt['job'], 'completed': False, 'next': 'report after process completion; no automatic retry'}


def report(context, row):
    refresh(context, row)
    current = unchanged(row)
    facts, results = row['facts'], []
    for index, check in enumerate(facts['definition']['checks']):
        attempts = [a for a in facts['attempts'] if a['check'] == index]
        latest = next((a for a in reversed(attempts) if a['hashes'] == current and 'info' in a), None)
        baseline = next((a['info'] for a in reversed(attempts) if a['phase'] == 'baseline' and 'info' in a), None)
        valid = latest and latest.get('probe', {}).get('base_revision') == facts['baseline']['base_revision']
        classification = state.classify(latest['info'], baseline) if valid else 'unverified'
        if latest and latest.get('probe_error') and latest['info'].get('exit_code') == 125:
            classification = 'environment_or_probe_failure'
        results.append({**check, 'classification': classification,
                        'process': latest['info'] if latest else None, 'output': latest['output'] if latest else None})
    verified = all(r['classification'] == 'passed' for r in results)
    diffs = scope_diff(context, row)
    artifact = context.artifacts.put(encode({'baseline': facts['baseline'], 'results': results, 'diffs': diffs}).encode(),
                                     source_ref=row['operation_id'] + ':review')
    return {'state': 'ready_for_review' if verified else 'incomplete', 'completed': False, 'verified': verified,
            'checks': results, 'scope': facts['definition']['scope'], 'base_revision': facts['baseline']['base_revision'],
            'dirty_preserved': facts['baseline']['dirty'], 'evidence': artifact.uri,
            'commit_paths': facts['definition']['scope'], 'limitations': 'No automatic commit/worktree removal; readonly offline snapshot; manual diff review required'}


def scope_diff(context, row):
    import difflib
    facts, result = row['facts'], {}
    for rel in facts['definition']['scope']:
        ref = facts['baseline']['originals'].get(rel)
        before = context.artifacts.get(*ref)[1] if ref else b''
        path = Path(facts['definition']['workspace']) / rel
        after = path.read_bytes() if path.exists() else b''
        result[rel] = ''.join(difflib.unified_diff(before.decode('utf-8', errors='replace').splitlines(True),
            after.decode('utf-8', errors='replace').splitlines(True), fromfile='before/' + rel, tofile='after/' + rel))
    return result


class SkillCancel:
    def __init__(self, context, cancel):
        self.context, self.cancel = context, cancel

    def is_set(self):
        return self.cancel.is_set() or not active(self.context) or not self.context.registry.is_enabled(NAME)

    async def wait(self):
        while not self.is_set():
            await asyncio.sleep(.05)
