"""Read-only diagnostics. Run from any cwd: python -B scripts/doctor.py [--json]."""
import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.doctor_checks import dependencies, permissions, services, switches  # noqa: E402
from scripts.doctor_evidence import evidence  # noqa: E402

SECTIONS = {'switches': '① 开关', 'dependencies': '② 依赖', 'services': '③ 服务',
            'permissions': '④ 权限', 'evidence': '⑤ 已验证行为'}


def item(key, status, message, *, remediation='', details=None):
    return {'id': key, 'status': status, 'message': message,
            'remediation': remediation, 'details': details or {}}


def diagnose(root=ROOT, config_path=None, *, online=False, timeout=3.0):
    root = Path(root).resolve()
    path = Path(config_path) if config_path else root / 'config/settings.json'
    if not path.is_absolute():
        path = root / path
    sections = {name: [] for name in SECTIONS}
    try:
        config = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(config, dict):
            raise ValueError('config_object_required')
        for key in ('memory', 'network', 'tools', 'skills', 'browser', 'providers', 'execution', 'orchestration'):
            if key in config and not isinstance(config[key], dict):
                raise ValueError('config_section_invalid')
        sections['switches'] = switches(config, item)
        if config.get('schema_version', 1) != 1:
            raise ValueError('schema2_requires_resolved_launch_paths')
        sections['dependencies'] = dependencies(root, config, item)
        sections['services'] = services(root, config, item, online, timeout)
        sections['permissions'] = permissions(root, config, item)
        sections['evidence'] = evidence(root, item)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        sections['switches'].append(item('config', 'blocked', type(exc).__name__,
            remediation='检查 --config 指定的 JSON 文件及各分区类型；恢复受版本控制的配置。'))
    statuses = [row['status'] for rows in sections.values() for row in rows]
    code = 2 if 'blocked' in statuses else int('warning' in statuses)
    return {'schema_version': 1, 'checked_at': datetime.now(UTC).isoformat(),
            'root': str(root), 'config_path': str(path), 'online': online, 'timeout_seconds': timeout,
            'scope': '指定配置文件静态诊断；不合并用户覆盖、不代表活跃会话策略；不写库验证、不启动服务。',
            'sections': sections, 'exit_code': code}


def render(report):
    lines = ['Vastar doctor', report['scope'], f"配置：{report['config_path']}"]
    for name, rows in report['sections'].items():
        lines.append('\n' + SECTIONS[name])
        for row in rows:
            icon = {'ok': '✅', 'warning': '⚠️', 'blocked': '❌'}[row['status']]
            lines.append(f"{icon} {row['id']}: {row['message']}")
            if row['details']:
                lines.append('  ' + json.dumps(row['details'], ensure_ascii=False))
            if row['remediation']:
                lines.append('  怎么修/下一步：' + row['remediation'])
    lines.append(f"\n退出码：{report['exit_code']}（0全绿 / 1警告或未验证 / 2阻断）")
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--online', action='store_true', help='显式允许只探测搜索与 MCP initialize；不调工具或模型')
    parser.add_argument('--config', type=Path, help='替代配置文件（不合并覆盖层）')
    parser.add_argument('--timeout', type=float, default=3.0, help='每项在线探测期限，默认3秒，最大3秒')
    args = parser.parse_args(argv)
    if not 0 < args.timeout <= 3:
        parser.error('--timeout 必须大于0且不超过3秒')
    report = diagnose(config_path=args.config, online=args.online, timeout=args.timeout)
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render(report))
    return report['exit_code']


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
