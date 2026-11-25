"""Worker arithmetic and an independent rational small-sample oracle."""
from decimal import Decimal, localcontext
from fractions import Fraction
from html import escape


def numeric(cells):
    return [Decimal(c['value']) for c in cells if c['kind'] == 'number']


def aggregate(cells):
    values = numeric(cells)
    with localcontext() as ctx:
        # Reserve every decimal place plus space for a carry across all rows.
        # This matters when a small transaction lies between two cancelling large ones.
        if values:
            ctx.prec = max(100, max(v.adjusted() for v in values) - min(v.as_tuple().exponent for v in values)
                           + len(str(len(values))) + 1)
        total = sum(values, Decimal(0))
        ctx.prec = 100
        return {'count': len(values), 'sum': str(total) if values else None,
                'mean': str(total / len(values)) if values else None,
                'min': str(min(values)) if values else None, 'max': str(max(values)) if values else None}


def grouped(rows, selection, units, cleaning_failures):
    """Keep source rows and literal typed keys. No inferred unit conversions."""
    key_col, val_col = selection['key_column'], selection['value_column']
    invalid = {f['source_row'] for f in cleaning_failures if f['column'] in (key_col, val_col)}
    groups, excluded = {}, []
    for row in rows:
        row_id, cells = row['source_row'], row['cells']
        reason = None
        if row_id in invalid:
            reason = '分组列或数值列清洗失败'
        elif len(cells) < max(key_col, val_col):
            reason = '缺少分组列或数值列'
        else:
            key, value = cells[key_col - 1], cells[val_col - 1]
            if key['kind'] not in {'text', 'date', 'number', 'boolean'}:
                reason = '缺失或不可用的分组值：' + key['kind']
            elif value['kind'] != 'number':
                reason = '非数值：' + value['kind']
        if reason:
            excluded.append({'source_row': row_id, 'reason': reason})
            continue
        identity = (key['kind'], key['value'], key.get('unit'), key.get('timezone'))
        groups.setdefault(identity, []).append((row_id, value))

    output = []
    for (kind, label, key_unit, timezone), items in groups.items():
        source_rows = [idx for idx, _ in items]
        values = [value for _, value in items]
        value_units = {v.get('unit', units.get(str(val_col), '未声明')) for v in values}
        entry = {'key': {'kind': kind, 'value': label, 'unit': key_unit, 'timezone': timezone},
                 'source_rows': source_rows}
        try:
            if len(value_units) != 1:
                raise ValueError('同组数值单位不一致')
            stats = aggregate(values)
            oracle = sum((Fraction(v['value']) for v in values), Fraction(0))
            if Fraction(stats['sum']) != oracle:
                raise ValueError('分组金额逐行分数复算不一致')
            entry.update(unit=next(iter(value_units)), aggregate=stats, verified_sum=True)
        except (ValueError, ArithmeticError) as exc:
            excluded.extend({'source_row': idx, 'reason': str(exc)} for idx in source_rows)
            entry.update(aggregate=None, verified_sum=False, error=str(exc))
        output.append(entry)
    covered = len(rows) - len(excluded)
    status = 'completed' if not excluded else 'partial' if covered else 'incomplete'
    return {**selection, 'status': status, 'input_rows': len(rows), 'included_rows': covered,
            'excluded': sorted(excluded, key=lambda e: e['source_row']), 'groups': output}


def crosscheck(cells):
    # Distinct implementation: rational arithmetic, selected raw records, no numeric()/aggregate() reuse.
    sample = cells[:17]
    count, total, values = 0, Fraction(0), []
    for item in sample:
        if item['kind'] != 'number':
            continue
        values.append(Fraction(item['value']))
        total += values[-1]
        count += 1
    primary = aggregate(sample)
    if primary['count'] != count or (count and Fraction(primary['sum']) != total):
        raise ValueError('独立小样本复算不一致；拒绝统计')
    if count:
        with localcontext() as ctx:
            ctx.prec = 100
            mean = total / count
            oracle_mean = Decimal(mean.numerator) / Decimal(mean.denominator)
        if (Decimal(primary['mean']) != oracle_mean or Fraction(primary['min']) != min(values)
                or Fraction(primary['max']) != max(values)):
            raise ValueError('独立均值/极值复算不一致')
    return {'method': 'independent Fraction loop', 'sample_positions': list(range(1, len(sample) + 1)),
            'sample': sample, 'count': count, 'sum_fraction': str(total), 'primary': primary, 'matched': True}


def chart(rows, col, unit, sheet):
    points = [(r['source_row'], Decimal(r['cells'][col]['value'])) for r in rows
              if col < len(r['cells']) and r['cells'][col]['kind'] == 'number'][:50]
    if not points:
        return None
    with localcontext() as ctx:
        ctx.prec = 100
        lo, hi = min(v for _, v in points), max(v for _, v in points)
        span = hi - lo or Decimal(1)
        coords = ' '.join(f'{60 + i * 500 / max(1, len(points)-1):.2f},{float(240 - (v-lo)/span*170):.2f}'
                          for i, (_, v) in enumerate(points))
    markers = ''.join(f'<circle cx="{pair.split(",")[0]}" cy="{pair.split(",")[1]}" r="3" fill="#2463a5"/>'
                      for pair in coords.split())
    scope = f'{sheet}；原始行 {points[0][0]}–{points[-1][0]}；数值样本 n={len(points)}；最多前 50 个数值'
    label = f'列 {col+1} [{unit}]'
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="640" height="340" viewBox="0 0 640 340">'
           '<rect width="640" height="340" fill="white"/>'
           '<path d="M60 60V250H580" fill="none" stroke="black"/>'
           f'<polyline points="{coords}" fill="none" stroke="#2463a5"/>{markers}'
           f'<text x="60" y="25">{escape(label)}</text>'
           f'<text x="5" y="65">{escape(str(hi))}</text><text x="5" y="245">{escape(str(lo))}</text>'
           f'<text x="60" y="275">X: 按原始行顺序的数值样本；Y: {escape(unit)}</text>'
           f'<text x="15" y="310">{escape(scope)}</text></svg>')
    return {'x_axis': '按原始行顺序的数值样本', 'y_axis': label, 'unit': unit,
            'sample_scope': scope, 'source_rows': [r for r, _ in points], 'svg': svg}


def analyze(rows, units):
    results, charts, failures = [], [], []
    width = max((len(r['cells']) for r in rows), default=0)
    for col in range(width):
        cells = [r['cells'][col] if col < len(r['cells']) else {'kind': 'blank', 'value': ''} for r in rows]
        present = {c.get('unit', units.get(str(col+1), '未声明')) for c in cells if c['kind'] == 'number'}
        try:
            if len(present) > 1:
                raise ValueError('同列数值单位不一致，禁止合并')
            unit = next(iter(present), units.get(str(col+1), '未声明'))
            check = crosscheck(cells)
            results.append({'column': col+1, 'unit': unit, 'aggregate': aggregate(cells), 'verification': check,
                            'excluded_non_numeric': len(cells)-sum(c['kind'] == 'number' for c in cells)})
            drawing = chart(rows, col, unit, '当前工作表')
            if drawing:
                charts.append(drawing)
        except (ValueError, ArithmeticError) as exc:
            failures.append({'column': col+1, 'reason': str(exc)})
    return results, charts, failures
