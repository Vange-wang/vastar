"""Reopen office outputs named by a committed operation and its owned artifact refs."""
import hashlib
import os
import re
import stat
from pathlib import Path


def _check_current_source(path, approved_hash, family):
    from core.tools.report_adapter import LIMITS as report_limits
    from core.tools.sheet_adapter import LIMITS as sheet_limits

    limit = (report_limits if family == 'report' else sheet_limits)['input_bytes']
    try:
        if path.resolve(strict=True) != path or not path.is_file():
            raise ValueError('输入来源路径已变化，无法按当前文件交付')
        with path.open('rb') as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_size > limit:
                raise ValueError('输入来源已变化或超过原分析的读取上限')
            raw = stream.read(limit + 1)
            current = path.stat()
            if (not os.path.samestat(opened, current) or opened.st_size != current.st_size
                    or opened.st_mtime_ns != current.st_mtime_ns or len(raw) != current.st_size
                    or path.resolve(strict=True) != path or hashlib.sha256(raw).hexdigest() != approved_hash):
                raise ValueError('输入来源已变化，与生成时的内容不符')
    except (OSError, RuntimeError) as exc:
        raise ValueError('输入来源已不可读取，无法核实该路径的当前版本') from exc


def office_outputs(artifacts, criterion, op):
    if set(criterion) - {'kind', 'tool', 'files', 'fields', 'group_by', 'source_path', 'source_sha256'}:
        raise ValueError('办公成果验收包含未支持的条件')
    tool = criterion['tool']
    types = {'generate_report': ('report', 'machine_verified'), 'analyze_spreadsheet': ('sheet', 'completed')}
    if tool not in types or op['tool'] != tool or op['state'] != 'committed':
        raise ValueError('缺少已提交的对应办公操作')
    family, status = types[tool]
    proof = op['facts'][family]
    approved = op['facts']['document']
    if not isinstance(approved, dict) or not isinstance(proof, dict):
        raise ValueError('办公操作缺少来源或验证明细')
    if 'fields' in criterion:
        table = approved.get('table')
        fields = [column['field'] for column in table['columns']] if isinstance(table, dict) else []
        if not fields or fields != criterion['fields']:
            raise ValueError('报告字段与批准验收的顺序不符')
    if 'group_by' in criterion:
        group = criterion['group_by']
        if (family != 'sheet' or not isinstance(group, dict) or set(group) != {'key_column', 'value_column'}
                or any(type(n) is not int or n < 1 for n in group.values())
                or approved.get('options', {}).get('group_by') != group):
            raise ValueError('分组与数值列不符合验收条件')
    source_hash = approved['source_hash']
    if (proof['status'] != status or proof['hashes']['unchanged'] is not True
            or proof['hashes']['before'] != source_hash or proof['hashes']['after'] != source_hash):
        raise ValueError('办公成果未完整核实')
    if not {'source_sha256', 'source_path'}.intersection(criterion):
        raise ValueError('验收需要指定输入来源')
    if 'source_sha256' in criterion and criterion['source_sha256'] != source_hash:
        raise ValueError('输入哈希与批准来源不符')
    if 'source_path' in criterion:
        source = Path(criterion['source_path'])
        if not source.is_absolute() or source != Path(approved['source']):
            raise ValueError('输入路径与批准来源不符')
        _check_current_source(source, source_hash, family)
    names = criterion['files']
    if not isinstance(names, list) or not names or any(not isinstance(n, str) for n in names) or len(set(names)) != len(names):
        raise ValueError('验收须列出唯一的成果文件名')
    staging = Path(proof['directory'])
    root = artifacts._require_dir().parent / (family + '-staging')
    if (staging.parent != root or not re.fullmatch('[0-9a-f]{32}', staging.name)
            or staging.resolve(strict=True) != staging):
        raise ValueError('办公交付目录不符')
    outputs = []
    for name in names:
        item = proof['deliverables'][name]
        filename = 'report.' + name if family == 'report' else name
        path = Path(item['path'])
        if Path(filename).name != filename or path != staging / filename or path.resolve(strict=True) != path:
            raise ValueError('办公成果路径不符')
        ref = item['artifact_ref']
        aid, version, _ = artifacts._parse_ref(ref)
        meta, _ = artifacts.get(aid, version)
        if (meta['uri'] != ref or meta['source_ref'] != f'{family}:{source_hash}:{name}'
                or proof['artifact_refs'][name] != ref or path.stat().st_size != meta['size']):
            raise ValueError('办公成果归档或字节数不符')
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if digest != meta['hash']:
            raise ValueError('办公成果已变化')
        outputs.append({'path': str(path), 'sha256': digest, 'artifact_ref': ref,
                        'operation_id': op['operation_id']})
    return outputs
