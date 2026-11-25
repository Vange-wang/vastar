"""把工具往返持久化为来源配对；敏感观察只保存脱敏占位，不存凭据。"""
import copy
import hashlib
import json

from core.artifacts import ArtifactError
from core.tools.audit import _redact
from core.tools.permissions import check_credential_path, is_env_path, resolve_read_path
from core.tools.sensitive_result import protected_result


def protected_call(call, base, tools=None):
    try:
        args = json.loads(call['function']['arguments'])
        from memory.policy import sensitive
        if call['function'].get('name') == 'write_memory' and sensitive(json.dumps(args, ensure_ascii=False)):
            return True
        for key in ('path', 'dest'):
            if key in args:
                if getattr(tools, 'project_inspection', False):
                    from core.one_shot import readable
                    if not readable(tools.project_root, tools.protected_paths, base / str(args[key])):
                        return True
                path = resolve_read_path(base, str(args[key]))
                if is_env_path(path) or check_credential_path(path):
                    return True
        return False
    except Exception:
        return True  # 无法核实的参数不把原文带入审计。


def _parse_arguments(raw):
    """tool_call 形状无保证（上游 round_trip 全防御式取值）：仅接受 str/JSON 对象。"""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def exchange_record(s, tools, calls, results):
    from memory.forget_buffers import entries_for_run
    memory = getattr(tools, 'memory_service', None)
    if memory is not None and entries_for_run(memory.store, getattr(getattr(s, 'request', None), 'run_id', None)):
        return {'messages': [], 'pair_status': 'incomplete', 'notes': ['memory_revoked']}
    saved_calls, saved_results, refs, notes, arguments = [], [], [], [], {}
    for i, call in enumerate(calls):
        safe = copy.deepcopy(call)
        if not isinstance(safe, dict):
            safe = {'id': None, 'type': 'function'}
            notes.append('tool_call 非对象，已按占位归档')
        function = call.get('function') if isinstance(call, dict) else None
        function = function if isinstance(function, dict) else {}
        raw_arguments = function.get('arguments')
        result_item = results[i] if i < len(results) else None
        observation_text = result_item.get("content", "") if isinstance(result_item, dict) else ""
        sensitive = (protected_call(call, tools.cwd or tools.project_root, tools)
                     or protected_result(function.get("name"), observation_text))
        parsed_arguments = _parse_arguments(raw_arguments)
        if parsed_arguments is None:
            # 缺 function/arguments 或参数 JSON 不可解析：归档脱敏占位并明说——
            # 不得硬索引崩溃，finally 内异常会掩盖工具结果并丢失 history 事件。
            sensitive = True
            notes.append('tool_call 缺 function/arguments 或参数不可解析，已按脱敏占位归档')
        if not isinstance(safe.get('function'), dict):
            safe['function'] = {}
        safe['function']['arguments'] = ('{"redacted":true}' if sensitive else
                                         json.dumps(_redact(parsed_arguments), ensure_ascii=False))
        if not sensitive:
            call_id = call.get('id') if isinstance(call, dict) else None
            if call_id is None:
                notes.append('tool_call 缺 id，参数未归档（不得据历史重放执行）')
            else:
                arguments[call_id] = preserve_arguments(tools, raw_arguments, safe, refs, notes)
        saved_calls.append(safe)
        if i >= len(results):
            continue
        observation = copy.deepcopy(results[i])
        if not isinstance(observation, dict):
            observation = {'role': 'tool', 'content': None}
            notes.append('tool 结果非对象，已按占位归档')
        content = observation.get('content')
        if not isinstance(content, str):
            content = '[观察缺失或形状异常]'
            notes.append('tool 结果缺 content，已按占位归档')
            observation['content'] = content
        if sensitive:
            observation['content'] = '[受保护观察：内容不持久化；需再次按权限读取]'
        elif tools.artifacts is not None:
            try:
                artifact = tools.artifacts.put(content.encode('utf-8'), source_ref='')
                refs.append(artifact.uri)
                # 只存短预览和不可变引用；完整观察由 artifact 保存。
                if len(content) > 1000:
                    observation['content'] = content[:1000] + '\n[大输出引用 ' + artifact.uri + ']'
            except (ArtifactError, OSError) as exc:
                notes.append(type(exc).__name__ + '：观察 artifact 不可用，保留本轮已交付文本')
        saved_results.append(observation)
    return {'messages': [{'role': 'assistant', 'content': '', 'tool_calls': saved_calls}, *saved_results],
            'artifact_refs': refs, 'model_call_ids': list(s.call_ledger)[-1:],
            'pair_status': 'complete' if len(results) == len(calls) else 'incomplete',
            'arguments': arguments, 'notes': notes,
            'arguments_policy': '长参数在受管产物，投影只存长度占位；不得据历史重放执行'}


def preserve_arguments(tools, raw_arguments, safe, refs, notes):
    # 调用处已校验可解析；这里只做 hash 与原文留存，不再硬索引 tool_call 形状。
    raw = raw_arguments if isinstance(raw_arguments, str) else json.dumps(raw_arguments, ensure_ascii=False)
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
