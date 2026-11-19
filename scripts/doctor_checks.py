"""Local checks do not instantiate runtime services, open a writable DB or launch programs."""
import hashlib
import importlib.metadata
import os
import re
import sys
import tomllib


def switches(config, make):
    rows = []
    for section, keys in {'orchestration': ('enabled',), 'network': ('enabled', 'allow_public_hosts'),
                          'execution': ('enabled', 'network'), 'browser': ('enabled', 'writes_enabled'),
                          'providers': ('enabled',)}.items():
        for key in keys:
            value = config.get(section, {}).get(key, False)
            status = 'ok' if value is True else 'warning' if value is False else 'blocked'
            rows.append(make(f'{section}.{key}', status,
                ('开（仅配置）' if value is True else '关' if value is False else '不是布尔值'),
                remediation=f'检查配置键 {section}.{key}；仅在需要该能力且获授权时调整。',
                details={'configured': value, 'usable': None}))
    rows.append(make('memory.long_term_enabled', 'warning', '历史兼容字段，不是当前记忆能力开关',
                     details={'configured': config.get('memory', {}).get('long_term_enabled')}))
    for section in ('tools', 'skills'):
        value = config.get(section, {}).get('enabled', [])
        rows.append(make(f'{section}.enabled', 'ok' if isinstance(value, list) and value else 'warning',
                         '启用名单（不表示逐项可用）', details={'configured': value}))
    return rows


def dependencies(root, config, make):
    rows = [make('python', 'ok' if sys.version_info >= (3, 11) else 'blocked', sys.version.split()[0],
                 remediation='使用 Python 3.11+ 创建项目 .venv。'),
            make('venv', 'ok' if sys.prefix != sys.base_prefix else 'warning',
                 '当前解释器在venv中' if sys.prefix != sys.base_prefix else '当前未使用venv',
                 remediation=r'使用 .venv\Scripts\python.exe -B scripts/doctor.py',
                 details={'executable': sys.executable})]
    try:
        project = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf8'))['project']
        specs = [(s, True) for s in project.get('dependencies', [])]
        specs += [(s, False) for s in project.get('optional-dependencies', {}).get('dev', [])]
        for spec, required in specs:
            match = re.fullmatch(r'([\w.-]+)==([\w.]+)', spec)
            if not match:
                rows.append(make('dependency:' + spec, 'warning', '未验证：此版本约束不支持静态精确比对'))
                continue
            name, expected = match.groups()
            try:
                actual = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                actual = None
            rows.append(make('dependency:' + name, 'ok' if actual == expected else 'blocked' if required else 'warning',
                f'安装版本={actual}；要求={expected}', remediation='在项目venv中执行 python -m pip install -r requirements.txt'))
    except (OSError, ValueError, KeyError) as exc:
        rows.append(make('pyproject', 'blocked', type(exc).__name__, remediation='恢复 pyproject.toml 并检查 project.dependencies。'))
    browser = config.get('browser', {})
    for key, suffix in [('node_executable', ''), ('playwright_package', 'package.json'),
                        ('chromium_directory', 'chrome-headless-shell.exe')]:
        raw = browser.get(key)
        target = (root / raw / suffix) if raw else None
        found = target is not None and target.is_file()
        rows.append(make('browser.' + key, 'ok' if found else 'warning',
            '文件存在，启动未验证' if found else '组件缺失/未配置',
            remediation=f'安装浏览器组件并核对 browser.{key}。', details={'path': str(target) if target else None}))
    try:
        from core.tools.report_adapter import resources
        manifest = resources(root / 'skills/report-generation/assets')
        rows.append(make('report.assets', 'ok', '报告组件哈希匹配；沙箱渲染未验证',
                         details={'files': len(manifest['files'])}))
    except (OSError, ValueError, KeyError) as exc:
        rows.append(make('report.assets', 'warning', f'报告可选组件未就绪：{type(exc).__name__}',
                         remediation='按 skills/report-generation/assets/manifest.json 补齐受控渲染包。'))
    rows.append(make('search.client', 'ok', '使用Python标准库HTTP客户端；SearXNG服务另检'))
    return rows


def services(root, config, make, online, timeout):
    rows = []
    raw = config.get('memory', {}).get('path')
    try:
        if not isinstance(raw, str) or not raw:
            raise ValueError('memory.path_missing')
        path = root / raw
        with path.open('rb') as stream:
            header = stream.read(100)
        if len(header) != 100 or header[:16] != b'SQLite format 3\x00':
            raise ValueError('sqlite_header_invalid')
        writable = os.access(path, os.W_OK) and os.access(path.parent, os.W_OK)
        rows.append(make('database', 'warning' if writable else 'blocked',
            '文件可读；实际写入/锁/SQL完整性未验证（严格只读）' if writable else '文件可读但OS写权限检查拒绝',
            remediation='由正常启动流程验证写入；如有故障先备份并核对 memory.path 和目录ACL。',
            details={'path': str(path), 'readable': True, 'writable_hint': os.access(path, os.W_OK),
                     'parent_writable_hint': os.access(path.parent, os.W_OK), 'write_verified': False,
                     'header_sha256': hashlib.sha256(header).hexdigest()}))
    except (OSError, ValueError, TypeError) as exc:
        rows.append(make('database', 'blocked', f'数据库缺失/不可读/头部无效：{type(exc).__name__}',
            remediation='核对配置键 memory.path；从已知备份恢复；新项目由正常 main.py 初始化，doctor 不创建。'))
    from scripts.doctor_online import service_checks
    return rows + service_checks(config, make, online, timeout)


def permissions(root, config, make):
    tools = config.get('tools', {})
    mode = tools.get('permission_mode', 'ask')
    rows = [make('tools.permission_mode', 'ok' if mode in ('auto', 'ask') else 'blocked', str(mode),
                 remediation='tools.permission_mode 只能为 auto 或 ask；auto 不覆盖硬拒。')]
    rows.append(make('timeouts', 'ok', '配置值（null=本地不限，不保证外部服务不限）', details={
        'tools.timeout_seconds': tools.get('timeout_seconds', 30),
        'tools.ask_timeout_s': tools.get('ask_timeout_s', 60),
        'network.timeout': config.get('network', {}).get('timeout', 10)}))
    rows.append(make('write_scope', 'warning', '没有全局任意写白名单；目标需逐操作判定，删除仍需审批', details={
        'project_root': str(root), 'source': 'core/tools/permissions.py; core/tools/registry.py',
        'protected': ['.git', 'config', 'logs', 'memory', '.vastar', '_backups', '_trash',
                      '.env及凭据家族', '系统目录', '实际data_dir与用户配置目录'],
        'session_scope': '会话工作目录/任务workspace/授权写路径不在此静态诊断范围'}))
    rows.append(make('network.policy', 'ok', '联网与沙箱执行分开授权', details={
        key: config.get('network', {}).get(key) for key in
        ('allowed_hosts', 'private_hosts', 'allowed_ports', 'proxy_mode', 'allow_public_hosts')}))
    return rows
