"""工具调用和全部回执不可分割；摘要输出是可验证的原文区间。"""


def groups(history):
    result, index = [], 0
    while index < len(history):
        message = history[index]
        if message.role == 'tool' or message.tool_call_id:
            raise ValueError('工具半配对：孤立回执；拒绝压缩')
        group = [message]
        if message.tool_calls:
            ids = [c['id'] for c in message.tool_calls]
            replies = history[index + 1:index + 1 + len(ids)]
            if (message.role != 'assistant' or len(ids) != len(set(ids))
                    or [r.tool_call_id for r in replies] != ids
                    or any(r.role != 'tool' or r.tool_calls or r.status != 'complete' for r in replies)):
                raise ValueError('工具半配对：缺失/重复/乱序回执；拒绝压缩')
            group.extend(replies)
        result.append(group)
        index += len(group)
    return result


def partition(history, recent_groups=2):
    grouped = groups(history)
    # 最近工具组 + 最近两条普通消息，短会话也允许产生交接包，但不拆工具组。
    tools = [g for g in grouped if g[0].tool_calls]
    kept = {m.message_id for g in tools[-recent_groups:] for m in g}
    kept.update(m.message_id for m in history[-2:])
    for group in grouped:
        if any(m.message_id in kept or m.status != 'complete' for m in group):
            kept.update(m.message_id for m in group)
    return ([m for m in history if m.message_id not in kept],
            [m for m in history if m.message_id in kept])


def summarize(background):
    """本地零请求摘要器：仅返回助手背景的逐字摘录区间，不改写事实。"""
    return [{'source': m.message_id, 'start': 0, 'end': min(96, len(m.content)),
             'text': m.content[:96]} for m in background]


def validate_summary(spans, background):
    sources = {m.message_id: m for m in background}
    if not isinstance(spans, list):
        raise ValueError('摘要器只允许返回带来源的背景摘录')
    for span in spans:
        if not isinstance(span, dict) or set(span) != {'source', 'start', 'end', 'text'}:
            raise ValueError('摘要含越权事实字段或不明来源')
        m = sources.get(span['source'])
        start, end = span['start'], span['end']
        if (m is None or type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(m.content)
                or span['text'] != m.content[start:end]):
            raise ValueError('矛盾/改写摘要或丢失引用；拒绝替换上版')


def validate_coverage(history, covered_ids):
    covered = set(covered_ids)
    for group in groups(history):
        count = sum(m.message_id in covered for m in group)
        if count not in (0, len(group)):
            raise ValueError('工具半配对：覆盖区间切断工具组')
