"""把工具往返持久化为来源配对；敏感观察只保存脱敏占位，不存凭据。"""
import copy
import hashlib
import json

from core.artifacts import ArtifactError
from core.tools.audit import _redact
from core.tools.permissions import check_credential_path, is_env_path, resolve_read_path
from core.tools.sensitive_result import protected_result


def protected_call(call, base):
    try:
        args = json.loads(call['function']['arguments'])
        from memory.policy import sensitive
        if call['function'].get('name') == 'write_memory' and sensitive(json.dumps(args, ensure_ascii=False)):
            return True
        for key in ('path', 'dest'):
            if key in args:
                path = resolve_read_path(base, str(args[key]))
                if is_env_path(path) or check_credential_path(path):
                    return True
        return False
    except Exception:
        return True  # 无法核实的参数不把原文带入审计。


def exchange_record(s, tools, calls, results):
    from memory.forget_buffers import entries_for_run
    memory = getattr(tools, 'memory_service', None)
    if memory is not None and entries_for_run(memory.store, getattr(getattr(s, 'request', None), 'run_id', None)):
        return {'messages': [], 'pair_status': 'incomplete', 'notes': ['memory_revoked']}
    saved_calls, saved_results, refs, notes, arguments = [], [], [], [], {}
    for i, call in enumerate(calls):
        safe = copy.deepcopy(call)
        observation_text = results[i].get("content", "") if i < len(results) else ""
        sensitive = (protected_call(call, tools.cwd or tools.project_root)
                     or protected_result(call["function"].get("name"), observation_text))
        safe.setdefault('function', {})
        safe['function']['arguments'] = ('{"redacted":true}' if sensitive else
                                         json.dumps(_redact(json.loads(call['function']['arguments'])), ensure_ascii=False))
        if not sensitive:
            arguments[call['id']] = preserve_arguments(tools, call, safe, refs, notes)
        saved_calls.append(safe)
        if i >= len(results):
            continue
        observation = copy.deepcopy(results[i])
        if sensitive:
            observation['content'] = '[受保护观察：内容不持久化；需再次按权限读取]'
        elif tools.artifacts is not None:
            try:
                artifact = tools.artifacts.put(observation['content'].encode('utf-8'), source_ref='')
                refs.append(artifact.uri)
                # 只存短预览和不可变引用；完整观察由 artifact 保存。
                if len(observation['content']) > 1000:
                    observation['content'] = observation['content'][:1000] + '\n[大输出引用 ' + artifact.uri + ']'
            except (ArtifactError, OSError) as exc:
                notes.append(type(exc).__name__ + '：观察 artifact 不可用，保留本轮已交付文本')
        saved_results.append(observation)
    return {'messages': [{'role': 'assistant', 'content': '', 'tool_calls': saved_calls}, *saved_results],
            'artifact_refs': refs, 'model_call_ids': list(s.call_ledger)[-1:],
            'pair_status': 'complete' if len(results) == len(calls) else 'incomplete',
            'arguments': arguments, 'notes': notes,
            'arguments_policy': '长参数在受管产物，投影只存长度占位；不得据历史重放执行'}


def preserve_arguments(tools, call, safe, refs, notes):
    raw = call['function']['arguments']
    record = {'hash': hashlib.sha256(raw.encode()).hexdigest(), 'artifact_ref': None}
    if json.loads(raw) == json.loads(safe['function']['arguments']):
        return record
    try:
        if tools.artifacts is None:
            raise ArtifactError('未装配')
        artifact = tools.artifacts.put(raw.encode('utf-8'), source_ref='')
        record['artifact_ref'] = artifact.uri
        refs.append(artifact.uri)
    except (ArtifactError, OSError):
        notes.append('长参数原文不可用：产物库未就绪；不得宣称可完整重放')
    return record


async def execute_captured(runner, s, calls, attempt_id, attempt_index):
    from core.budget_topup.tool_runtime import execute_tools
    results = []
    try:
        return await execute_tools(runner, s, calls, attempt_id, attempt_index, results)
    finally:
        if getattr(runner._tools, 'history_service', None) is not None:
            payload = exchange_record(s, runner._tools, calls, results)
            await s.emit(s.factory.build('history.tool_exchange', payload, channel='status', attempt_id=attempt_id))
