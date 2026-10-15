"""Exclude generated trees from broad discovery; explicit scopes remain readable."""
import os
from pathlib import Path

from core.tools.registry import ToolParamError


class DiscoveryScope:
    def __init__(self, args, root, project_root, protected):
        include = args.get('include_generated', False)
        if not isinstance(include, bool):
            raise ToolParamError('include_generated 必须为布尔值')
        self.include = include
        self.root = Path(root)
        self.project_root = Path(project_root or root)
        self.protected = protected
        self.generated = set()
        self.notes = {'root': self.root, 'protected': set(), 'unreadable': {}}

    def __call__(self, path):
        if self.protected is not None and self.protected(path):
            self.notes['protected'].add(str(path))
            return True
        if self.include or path == self.root:
            return False
        name = path.name.lower() if os.name == 'nt' else path.name
        cache = name in {'.work', '__pycache__', '.pytest_cache', '.ruff_cache',
                         '.mypy_cache', 'node_modules', 'venv', '.tox'}
        generated = any(path == self.project_root / relative for relative in
                        ('logs', 'memory/artifacts', 'memory/_backups', 'memory/_trash'))
        if (cache or generated) and path.is_dir():
            self.generated.add(path)
            return True
        return False

    def note(self):
        if not self.generated:
            return ''
        return (f'默认跳过生成目录 {len(self.generated)} 个（临时夹具、日志归档、产物和缓存）；'
                '如需查这些内容，请显式指定 path 或 include_generated=true')


INCLUDE_GENERATED = {'type': 'boolean', 'description':
                    '默认false，跳过临时夹具/日志归档/产物/缓存；true纳入这些目录，保护路径仍跳过。'}
