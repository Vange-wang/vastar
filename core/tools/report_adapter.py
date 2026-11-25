"""Pinned external renderer kit staged into a readonly offline AppContainer."""
import hashlib
import json
import shutil
import zipfile
from pathlib import Path

from core.tools.document_adapter import fingerprint
from core.tools.report_source import validate_source

ASSETS = Path(__file__).resolve().parents[2] / "skills" / "report-generation" / "assets"
LIMITS = {"input_bytes": 1024 * 1024, "output_bytes": 48 * 1024 * 1024,
          "memory_bytes": 768 * 1024 * 1024, "wall_seconds": 90}


class ReportTableError(ValueError):
    """An input or projection problem the caller can correct."""


def _unique_fields(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ReportTableError('原始 JSON 存在重复字段，无法确定表格内容')
    return result


def project_table(raw, selection, name, digest):
    if not isinstance(selection, dict) or set(selection) != {'title', 'columns'}:
        raise ReportTableError('表格报告需要 title 和 columns')
    title, columns = selection['title'], selection['columns']
    if not isinstance(title, str) or not title.strip() or len(title) > 100:
        raise ReportTableError('表格标题需要 1 至 100 个字符')
    if not isinstance(columns, list) or not 1 <= len(columns) <= 5:
        raise ReportTableError('表格需要选择 1 至 5 列')
    for col in columns:
        if (not isinstance(col, dict) or set(col) != {'field', 'label'}
                or any(not isinstance(v, str) or not v.strip() or len(v) > 100 for v in col.values())):
            raise ReportTableError('每列需要有效的来源字段 field 和显示名称 label')
    fields = [col['field'] for col in columns]
    labels = [col['label'] for col in columns]
    if len(set(fields)) != len(fields) or len(set(labels)) != len(labels):
        raise ReportTableError('来源字段和显示名称都应唯一')
    # Preserve the literal representation of JSON numbers, including exponent notation and trailing zeroes.
    records = json.loads(raw, parse_int=str, parse_float=str, object_pairs_hook=_unique_fields)
    if not isinstance(records, list) or len(records) > 150:
        raise ReportTableError('表格输入需要最多 150 条记录的 JSON 对象数组')
    if not records:
        raise ReportTableError('原始记录数组为空，无法核实所选字段')
    rows = []
    for i, record in enumerate(records, 1):
        row = []
        for field in fields:
            if not isinstance(record, dict) or field not in record:
                raise ReportTableError(f'第 {i} 条记录缺少字段 {field}')
            value = record[field]
            if value is None or isinstance(value, bool):
                value = json.dumps(value)
            if not isinstance(value, str) or len(value) > 160:
                raise ReportTableError(f'第 {i} 条记录的 {field} 需要一个不超过 160 字符的标量')
            row.append(value)
        rows.append(row)
    return {'title': title, 'paragraphs': [{'text': f'来源文件共有 {len(records)} 条记录，表中保留原始顺序。',
                                          'source': 'S1'}],
            'tables': [{'headers': labels, 'rows': rows, 'source': 'S1', 'unit': ''}],
            'references': [{'id': 'S1', 'citation': f'{name}，SHA-256 {digest}'}]}


def resources(root=ASSETS):
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    for name, expected in manifest["files"].items():
        if Path(name).name != name or hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
            raise ValueError("受控字体/模板/渲染组件缺失或版本不一致")
    for name, expected in manifest.get("renderer_sources", {}).items():
        code = Path(__file__).with_name(name).read_text(encoding="utf-8").encode()
        if hashlib.sha256(code).hexdigest() != expected:
            raise ValueError("渲染脚本版本不一致")
    return manifest


class ReportPayload:
    private_output = True

    def __init__(self, path, staging, table=None):
        self.path, self.staging = Path(path), Path(staging)
        with self.path.open('rb') as stream:
            raw = stream.read(LIMITS['input_bytes'] + 1)
        if len(raw) > LIMITS["input_bytes"]:
            raise ValueError("报告源超过 1 MiB")
        self.before = hashlib.sha256(raw).hexdigest()
        self.staging.mkdir(parents=True, exist_ok=False)
        pending = self.staging / "source.json.partial"
        pending.write_bytes(raw)
        pending.replace(self.staging / "source.json")
        (self.staging / "STATUS.txt").write_text("未完成输出\n", encoding="utf-8")
        source = json.loads(raw) if table is None else project_table(raw, table, self.path.name, self.before)
        if table is None and isinstance(source, list):
            raise ReportTableError('来源是 JSON 记录数组。请用 table.title 和 table.columns 选择来源字段与列名')
        self.source = validate_source(source)
        self.versions = resources()
        self.manifest = {"source_hash": self.before, "source": str(path), "versions": self.versions,
                         "limits": dict(LIMITS)}
        if table is not None:
            self.manifest['table'] = {'title': table['title'], 'columns': [dict(col) for col in table['columns']]}
        self.script = ".\\renderer\\python.exe -I -B -X utf8 .\\report_render.py; exit $LASTEXITCODE"
        self.approval_lines = ["只读来源，私有暂存生成；验证通过才发布，不覆盖原件。",
                               "外部依赖、非内置：" + self.versions["renderer_version"],
                               "离线 AppContainer；原始输出不写进程 artifact；真人版式待用户。"]
        if table is not None:
            self.approval_lines.append('表格标题与显示列：' + json.dumps(self.manifest['table'], ensure_ascii=False))

    def stage(self, profile):
        resources()
        if fingerprint(self.path) != self.before:
            raise ValueError("source changed")
        with zipfile.ZipFile(ASSETS / "renderer-win-x64.zip") as archive:
            archive.extractall(profile.work / "renderer")
        for name in ("NotoSansSC.ttf", "template.json"):
            shutil.copy2(ASSETS / name, profile.work / name)
        for name in ("report_render", "report_docx", "report_layout", "report_source", "report_images"):
            shutil.copy2(Path(__file__).with_name(name + ".py"), profile.work / (name + ".py"))
        (profile.work / "source.json").write_text(json.dumps(self.source, ensure_ascii=False), encoding="utf-8")
        profile.memory_bytes = LIMITS["memory_bytes"]

    def verify_unchanged(self):
        after = fingerprint(self.path)
        if after != self.before:
            raise ValueError("source changed; publication refused")
        return {"before": self.before, "after": after, "unchanged": True}
