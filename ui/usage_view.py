"""Human usage explanations; missing accounting is never converted to zero."""


def tokens_text(usage):
    parts = []
    for key, label in (('input_tokens', '输入'), ('output_tokens', '输出')):
        value = usage.get(key)
        parts.append(f'{label} {value}' if isinstance(value, (int, float)) and not isinstance(value, bool)
                     else f'{label}用量未返回')
    return ' · '.join(parts)


def call_usage(record):
    usage = record.get('actual_usage') or {}
    if any(isinstance(v, (int, float)) and not isinstance(v, bool) for v in usage.values()):
        return '实际 ' + tokens_text(usage)
    error = record.get('error') or {}
    details = error.get('details') or {}
    if details.get('connection_established') is False:
        return '网络未连通，未产生用量'
    if record.get('started') is False:
        return '请求未发出，未产生用量'
    return '供应商未返回用量，无法核实实际消耗'


def run_usage(totals, records, calls):
    if totals and all(isinstance(totals.get(k), (int, float))
                      for k in ('input_tokens', 'output_tokens')):
        return f"入{totals['input_tokens']}/出{totals['output_tokens']}"
    if records:
        reasons = list(dict.fromkeys(call_usage(r) for r in records))
        return '；'.join(reasons)
    if calls or totals:
        return '供应商未返回用量，无法核实实际消耗'
    return ''


def operation_state(state):
    return '待核实（动作结果尚未确认）' if state == 'unknown' else state
