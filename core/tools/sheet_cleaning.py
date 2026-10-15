"""Explicit worker cleaning; before/after audit for every changed cell or removed row."""
import copy
from collections import Counter
from datetime import datetime
from decimal import Decimal, localcontext

from sheet_cells import MONEY, characteristics, identity, number


def profile(rows):
    width = max((len(r['cells']) for r in rows), default=0)
    fields = []
    seen, duplicates = set(), []
    for row in rows:
        key = identity(row)
        if key in seen:
            duplicates.append(row['source_row'])
        seen.add(key)
    for col in range(width):
        values = [r['cells'][col] if col < len(r['cells']) else {'kind': 'blank', 'value': ''} for r in rows]
        notes = [characteristics(c) for c in values]
        fields.append({'column': col + 1, 'types': dict(Counter(c['kind'] for c in values)),
                       'missing': sum(c['kind'] == 'blank' for c in values),
                       'units': sorted({u for n in notes for u in n['units']}),
                       'timezones': sorted({n['timezone'] for n in notes if n['timezone']}),
                       'ambiguous_dates': sum(n['ambiguous_date'] for n in notes)})
    return {'fields': fields, 'duplicate_source_rows': duplicates, 'row_count': len(rows)}


def transform(item, rule):
    if item['kind'] in {'formula', 'error', 'blank'}:
        return item
    result = dict(item)
    if rule['op'] == 'trim':
        result['value'] = item['value'].strip()
        if result['value'] == '':
            result['kind'] = 'blank'
    elif rule['op'] == 'decimal':
        match = MONEY.fullmatch(item['value'])
        if match is None:
            raise ValueError('非声明的金额/数字格式')
        currency, scale = match['currency'] or '', match['scale'] or ''
        if currency != rule.get('currency', '') or scale != rule.get('scale', ''):
            raise ValueError('货币或数量级与显式规则不符')
        raw = match['n']
        if ',' in raw and __import__('re').fullmatch(r'[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?', raw) is None:
            raise ValueError('千分位格式不明确')
        value = number(raw.replace(',', ''))
        if value is None:
            raise ValueError('数字超出精度/指数预算')
        with localcontext() as ctx:
            ctx.prec = 100
            result.update(value=str(value * Decimal({'': 1, '千': 1000, '万': 10000, '亿': 100000000}[scale])),
                          kind='number', unit=rule['unit'])
    elif rule['op'] == 'date':
        value = datetime.strptime(item['value'], rule['format'])
        result.update(value=value.isoformat(), kind='date', timezone=rule['timezone'])
    return result


def clean(rows, rules):
    result, changes, failures, stats = copy.deepcopy(rows), [], [], []
    for index, rule in enumerate(rules):
        touched, count, removed = set(), 0, []
        if rule['op'] == 'drop_duplicates':
            kept, seen = [], set()
            for row in result:
                key = identity(row)
                if key in seen:
                    removed.append(row['source_row'])
                    changes.append({'rule': index, 'removed_row': row})
                else:
                    seen.add(key)
                    kept.append(row)
            result = kept
        else:
            col = rule['column'] - 1
            for row in result:
                if col >= len(row['cells']):
                    continue
                before = row['cells'][col]
                try:
                    after = transform(before, rule)
                    if after != before:
                        row['cells'][col] = after
                        changes.append({'rule': index, 'source_row': row['source_row'], 'column': col + 1,
                                        'before': before, 'after': after})
                        touched.add(row['source_row'])
                        count += 1
                except (ValueError, KeyError) as exc:
                    failures.append({'rule': index, 'source_row': row['source_row'], 'column': col + 1,
                                     'reason': str(exc), 'value_retained': before})
        stats.append({'rule': rule, 'changed_rows': len(touched), 'changed_cells': count,
                      'deleted_rows': removed, 'deleted_row_count': len(removed)})
    return result, {'rules': rules, 'statistics': stats, 'changes': changes, 'failures': failures}
