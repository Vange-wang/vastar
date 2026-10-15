"""历史还原：原始消息 + 成组工具轨迹；不把旧摘要猜成工具调用。"""
import json

from core.context import SourceMessage


def context_history(store, session_id):
    rows, cursor = [], 0
    while True:
        page = store.history_page(session_id, after=cursor)
        rows.extend(page['items'])
        if page['next_cursor'] is None:
            break
        cursor = page['next_cursor']
    # 流式助手消息可能先于工具投影入库；同一 Run 的最终答复放在工具配对之后。
    exchanges = {}
    for row in rows:
        if row['kind'] in ('tool_exchange', 'legacy_tool_event'):
            exchanges.setdefault(row['run_id'], []).append(row)
    result, emitted = [], set()
    for row in rows:
        if row['kind'] != 'message':
            continue
        with store._lock:
            message = store._conn.execute('SELECT * FROM messages WHERE message_id=?', (row['source_id'],)).fetchone()
        if message is None:
            continue
        if message['role'] == 'assistant':
            for exchange in exchanges.get(row['run_id'], []):
                result.extend(trace_messages(exchange))
                emitted.add(exchange['source_id'])
        result.append(SourceMessage(message_id=row['source_id'], role=message['role'],
                                    content=message['content'], reasoning_content=message['reasoning_content'],
                                    status=message['status'], model_call_ids=[c['call_id'] for c in row['model_calls']]))
    for row in rows:
        if row['kind'] in ('tool_exchange', 'legacy_tool_event') and row['source_id'] not in emitted:
            result.extend(trace_messages(row))
    return result


def trace_messages(row):
    if row['kind'] == 'legacy_tool_event':
        return [SourceMessage(row['source_id'], 'assistant',
                              '[旧工具审计事件：非完整配对，不可重放；来源 ' + row['source_id'] + ']\n' + row['body'])]
    return exchange_messages(row)


def exchange_messages(row):
    p = row['record']
    if p.get('pair_status') == 'incomplete':
        return [SourceMessage(row['source_id'], 'assistant',
                              '[历史工具往返未完成；不得视作执行成功。原始请求及已有回执见历史来源 ' + row['source_id'] + ']')]
    messages = p['messages']
    calls = messages[0].get('tool_calls', []) if messages else []
    if not calls or [c['id'] for c in calls] != [m.get('tool_call_id') for m in messages[1:]]:
        raise ValueError('历史工具配对损坏；拒绝猜补')
    return [SourceMessage(message_id=f"{row['source_id']}:{i}", role=m['role'],
                          content=m.get('content') or '', tool_calls=m.get('tool_calls'),
                          tool_call_id=m.get('tool_call_id'), model_call_ids=p.get('model_call_ids', []),
                          artifact_refs=p.get('artifact_refs', [])) for i, m in enumerate(messages)]


def wire_message(message):
    wire = {'role': message.role, 'content': message.content}
    if message.tool_calls is not None:
        wire['tool_calls'] = message.tool_calls
    if message.tool_call_id is not None:
        wire['tool_call_id'] = message.tool_call_id
    return wire


def wire_text(message):
    return json.dumps(wire_message(message), ensure_ascii=False) if message.tool_calls or message.tool_call_id else message.content
