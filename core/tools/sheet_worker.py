"""Isolated stdlib spreadsheet entry point. No shell, formula evaluator or network imports."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sheet_analysis import analyze  # noqa: E402
from sheet_cleaning import clean, profile  # noqa: E402
from sheet_reader import read  # noqa: E402


def run(request):
    sheets, limitations = read('source', request['kind'])
    results = []
    for sheet in sheets:
        rows = sheet['rows']
        header = rows[:1] if request['options']['header'] else []
        data = rows[1:] if header else rows
        identification = profile(data)
        cleaned, method = clean(data, request['options']['rules'])
        stats, charts, failures = analyze(cleaned, request['options']['units'])
        for drawing in charts:
            drawing['sample_scope'] = sheet['name'] + ': ' + drawing['sample_scope']
        results.append({**sheet, 'header': header, 'identification': identification, 'cleaned': cleaned,
                        'method': method, 'results': stats, 'charts': charts, 'analysis_failures': failures})
    return {'status': 'partial' if any(s.get('error') or s['method']['failures'] or s['analysis_failures']
                                      for s in results) else 'completed',
            'sheets': results, 'limitations': limitations + ['描述性统计，不从相关性推导因果；因果待独立证据',
                '只分析有界单元格；隐藏表明确列出，不执行任何公式/宏/外链；未声明单位不推断'],
            'network_requests': 0, 'formula_execution': False, 'method_version': 'stdlib-sheet-v1'}


if __name__ == '__main__':
    try:
        output = run(json.loads(Path('request.json').read_text(encoding='utf-8')))
    except Exception as exc:
        output = {'status': 'incomplete', 'error': str(exc), 'retained': 'frozen source copy'}
    raw = json.dumps(output, ensure_ascii=False)
    if len(raw.encode('utf-8')) > 16 * 1024 * 1024:
        raw = json.dumps({'status': 'incomplete', 'error': '结果超过 16 MiB；原始副本保留'}, ensure_ascii=False)
    print(raw)
