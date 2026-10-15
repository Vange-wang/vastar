"""Freeze original bytes and stage only the copy into B27/B28's offline sandbox."""
import hashlib
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

from core.tools.document_adapter import DocumentPayload, fingerprint

LIMITS = {'input_bytes': 8 * 1024 * 1024, 'output_bytes': 16 * 1024 * 1024,
          'memory_bytes': 512 * 1024 * 1024, 'wall_seconds': 30}


def options(args):
    if set(args) - {'path', 'header', 'rules', 'units'} or not isinstance(args.get('path'), str):
        raise ValueError('需要 path；仅支持 header/rules/units')
    header, rules, units = args.get('header', True), args.get('rules', []), args.get('units', {})
    if type(header) is not bool or not isinstance(rules, list) or len(rules) > 16 or not isinstance(units, dict):
        raise ValueError('无效分析选项')
    if any(not isinstance(k, str) or not k.isdigit() or not 1 <= int(k) <= 64
           or not isinstance(v, str) or not v or len(v) > 64 for k, v in units.items()):
        raise ValueError('units 必须是 1-based 列号到明确单位的映射')
    allowed = {'trim': {'op', 'column'}, 'drop_duplicates': {'op'},
               'decimal': {'op', 'column', 'unit', 'currency', 'scale'},
               'date': {'op', 'column', 'format', 'timezone'}}
    for rule in rules:
        if not isinstance(rule, dict) or rule.get('op') not in allowed or set(rule) - allowed[rule['op']]:
            raise ValueError('不支持的清洗规则')
        if rule['op'] != 'drop_duplicates' and (type(rule.get('column')) is not int or not 1 <= rule['column'] <= 64):
            raise ValueError('规则 column 必须在 1..64')
        if rule['op'] == 'decimal' and (not isinstance(rule.get('unit'), str) or not rule['unit']
                or rule.get('currency', '') not in ('', '$', '¥', '￥', '€', '£', 'USD', 'CNY', 'EUR', 'GBP')
                or rule.get('scale', '') not in ('', '千', '万', '亿')):
            raise ValueError('金额规则需单位/货币/数量级')
        if rule['op'] == 'date' and (rule.get('format') not in ('%d/%m/%Y', '%m/%d/%Y', '%Y-%m-%d')
                                     or rule.get('timezone') not in ('unknown', 'UTC')):
            raise ValueError('日期格式与时区必须显式（unknown/UTC）')
    if len(json.dumps(args)) > 16384:
        raise ValueError('选项过大')
    return {'header': header, 'rules': rules, 'units': units}


class SheetPayload:
    private_output = True

    def __init__(self, path, staging, settings):
        self.path, self.staging = Path(path), Path(staging)
        self.kind = self.path.suffix.lower().lstrip('.')
        if self.kind not in ('csv', 'xlsx') or self.path.stat().st_size > LIMITS['input_bytes']:
            raise ValueError('仅支持不超过 8 MiB 的 UTF-8 CSV / 无宏 XLSX')
        self.staging.mkdir(parents=True, exist_ok=False)
        self.frozen = self.staging / ('source.' + self.kind)
        with self.frozen.open('wb') as target:
            self.before = fingerprint(self.path, target)
        if self.frozen.stat().st_size > LIMITS['input_bytes']:
            raise ValueError('复制时输入超过 8 MiB')
        self.verify_unchanged()
        (self.staging / 'STATUS.txt').write_text('未完成分析\n', encoding='utf-8')
        self.manifest = {'source': str(self.path), 'source_hash': self.before, 'kind': self.kind,
                         'options': settings, 'limits': dict(LIMITS), 'method_version': 'stdlib-sheet-v1',
                         'runtime': 'application CPython stdlib ' + sys.version.split()[0],
                         'worker_sha256': {name: hashlib.sha256(Path(__file__).with_name(name + '.py').read_bytes()).hexdigest()
                             for name in ('sheet_worker', 'sheet_reader', 'sheet_cells', 'sheet_cleaning', 'sheet_analysis')}}
        self.script = '.\\python\\python.exe -I -B -X utf8 .\\sheet_worker.py; exit $LASTEXITCODE'
        self.approval_lines = ['只分析冻结副本；不执行公式/宏/外链；不覆盖原件。',
                               '离线 AppContainer，512 MiB、30 秒、16 MiB 输出。',
                               '显式清洗规则：' + json.dumps(settings, ensure_ascii=False)]

    def stage(self, profile):
        self.verify_unchanged()
        # Reuse B31's application-stdlib runtime copier, without host site-packages.
        proxy = SimpleNamespace(path=self.frozen, before=self.before, kind=self.kind, manifest=self.manifest)
        DocumentPayload.stage(proxy, profile)
        for name in ('sheet_worker', 'sheet_reader', 'sheet_cells', 'sheet_cleaning', 'sheet_analysis'):
            shutil.copy2(Path(__file__).with_name(name + '.py'), profile.work / (name + '.py'))

    def verify_unchanged(self):
        after, copy_hash = fingerprint(self.path), fingerprint(self.frozen)
        if after != self.before or copy_hash != self.before:
            raise ValueError('原始表格或冻结副本 hash 变化；拒绝结果')
        return {'before': self.before, 'after': after, 'copy_hash': copy_hash, 'unchanged': True}
