"""Package checked sheet results as plain Excel cells with no active content."""
import io
import re
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

MAIN = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PKG = 'http://schemas.openxmlformats.org/package/2006/relationships'
TYPES = 'http://schemas.openxmlformats.org/package/2006/content-types'
MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.'
XML_ESCAPE = re.compile(r'_x[0-9a-fA-F]{4}_|[\x00-\x08\x0b-\x0d\x0e-\x1f\ufffe\uffff]')
PLAIN_NUMBER = re.compile(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?\Z')


def _cell(ref, value):
    kind, text = (value['kind'], value['value']) if isinstance(value, dict) else ('text', str(value))
    if kind == 'boolean' and text in ('0', '1'):
        return f'<c r="{ref}" t="b"><v>{text}</v></c>'
    # Excel retains only 15 significant decimal digits. Keep other representations literal.
    digits = text.replace('-', '').replace('.', '').lstrip('0')
    if kind == 'number' and PLAIN_NUMBER.fullmatch(text) and len(digits) <= 15 and text != '-0':
        return f'<c r="{ref}"><v>{text}</v></c>'
    text = XML_ESCAPE.sub(lambda m: '_x005F_' + m[0][1:] if m[0][0] == '_' else f'_x{ord(m[0]):04X}_', text)
    return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(text)}</t></is></c>'


def _letters(index):
    result = ''
    while index:
        index, digit = divmod(index - 1, 26)
        result = chr(65 + digit) + result
    return result


def _worksheet(archive, target, rows, header):
    with archive.open('xl/' + target, 'w') as output:
        start = f'<worksheet xmlns="{MAIN}">'
        if header:
            start += ('<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
                      'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>')
        output.write((start + '<sheetFormatPr defaultColWidth="23" defaultRowHeight="15"/><sheetData>').encode())
        for index, cells in enumerate(rows, 1):
            row = ''.join(_cell(_letters(n) + str(index), cell) for n, cell in enumerate(cells, 1))
            output.write(f'<row r="{index}">{row}</row>'.encode())
        output.write(b'</sheetData></worksheet>')


def _index(result, source, digest):
    yield ['来源工作表', '原可见性', '清洗数据页', '分组页', '清洗后行数', '分析状态或问题']
    for n, sheet in enumerate(result['sheets'], 1):
        error = sheet.get('error')
        grouped = sheet.get('grouped')
        issues = [f['reason'] for f in sheet['analysis_failures'] + sheet['method']['failures']]
        yield [sheet['name'], sheet['visibility'], '' if error else f'数据 {n}', f'分组 {n}' if grouped else '',
               '' if error else len(sheet['cleaned']), error or '、'.join(dict.fromkeys(issues)) or '已核实']
    yield []
    yield ['来源文件', source]
    yield ['来源 SHA-256', digest]
    yield ['分析状态', result['status']]
    yield ['单元格说明', '公式以文本保留，长数值保留精度。原行号、清洗前后和复算记录见 analysis.json。']


def _group_rows(summary):
    yield ['分组值', '类型', '分组值单位', '分组值时区', '计入行数', '合计', '数值单位', '说明']
    for group in summary['groups']:
        key = group['key']
        stats = group['aggregate']
        yield [key['value'], key['kind'], key['unit'] or '', key['timezone'] or '',
               {'kind': 'number', 'value': str(stats['count'])} if stats else '',
               {'kind': 'number', 'value': stats['sum']} if stats else '', group.get('unit', ''),
               '合计已复核' if stats else group['error']]
    yield []
    yield ['分组状态', summary['status'], summary.get('error', '')]
    yield ['清洗后行数', summary['input_rows'], '计入行数', summary['included_rows']]
    if summary['excluded']:
        yield ['未计入的原行号', '原因']
        for item in summary['excluded']:
            yield [item['source_row'], item['reason']]


def _data_rows(sheet):
    for row in sheet['header']:
        yield [cell['value'] for cell in row['cells']]
    for row in sheet['cleaned']:
        yield row['cells']


def workbook(result, source, digest):
    pages = [('说明', 'visible', _index(result, source, digest), True)]
    for i, sheet in enumerate(result['sheets'], 1):
        if sheet.get('error'):
            continue
        state = sheet['visibility'] if sheet['visibility'] in ('hidden', 'veryHidden') else 'visible'
        pages.append((f'数据 {i}', state, _data_rows(sheet), bool(sheet['header'])))
        if sheet.get('grouped'):
            pages.append((f'分组 {i}', state, _group_rows(sheet['grouped']), True))
    data = io.BytesIO()
    with ZipFile(data, 'w', compression=ZIP_DEFLATED) as package:
        sheets, rels, content = [], [], []
        for i, (name, state, rows, header) in enumerate(pages, 1):
            target = f'worksheets/sheet{i}.xml'
            _worksheet(package, target, rows, header)
            sheets.append(f'<sheet name="{name}" sheetId="{i}" state="{state}" r:id="r{i}"/>')
            rels.append(f'<Relationship Id="r{i}" Type="{REL}/worksheet" Target="{target}"/>')
            content.append(f'<Override PartName="/xl/{target}" ContentType="{MIME}worksheet+xml"/>')
        package.writestr('xl/workbook.xml', f'<workbook xmlns="{MAIN}" xmlns:r="{REL}"><bookViews><workbookView/>'
                         '</bookViews><sheets>' + ''.join(sheets) + '</sheets></workbook>')
        package.writestr('xl/_rels/workbook.xml.rels', f'<Relationships xmlns="{PKG}">' + ''.join(rels)
                         + '</Relationships>')
        package.writestr('_rels/.rels', f'<Relationships xmlns="{PKG}"><Relationship Id="r1" Type="{REL}/'
                         'officeDocument" Target="xl/workbook.xml"/></Relationships>')
        package.writestr('[Content_Types].xml', f'<Types xmlns="{TYPES}"><Default Extension="rels" ContentType='
                         '"application/vnd.openxmlformats-package.relationships+xml"/><Override PartName="/xl/'
                         f'workbook.xml" ContentType="{MIME}sheet.main+xml"/>' + ''.join(content) + '</Types>')
    return data.getvalue()
