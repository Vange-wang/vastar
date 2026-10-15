"""Bounded, explicit repository copies; no Git or test process runs on the host."""
import hashlib
import shutil
from pathlib import Path
from types import SimpleNamespace

from core.tools.document_adapter import DocumentPayload
from core.tools.permissions import check_credential_path, is_env_path

OMIT = {'.venv', 'node_modules', '__pycache__', '.pytest_cache', 'logs'}
LIMITS = {'input_bytes': 64 * 1024 * 1024, 'output_bytes': 4 * 1024 * 1024,
          'memory_bytes': 768 * 1024 * 1024, 'wall_seconds': 120}


def require(value, code):
    if not value:
        raise ValueError(code)


def regular(path):
    return not path.is_symlink() and not (getattr(path.lstat(), 'st_file_attributes', 0) & 0x400)


def files(root):
    root = Path(root)
    result, omitted, pending, size = {}, [], [root], 0
    while pending:
        parent = pending.pop()
        for path in sorted(parent.iterdir()):
            rel = path.relative_to(root).as_posix()
            if rel == '.git':
                continue
            if (check_credential_path(path) or path.name in OMIT or path.name.startswith('.env')
                    or is_env_path(path) or path.suffix.lower() in {'.db', '.sqlite', '.sqlite3'}):
                omitted.append(rel + ('/' if path.is_dir() else ''))
                continue
            require(regular(path), 'code_reparse_refused')
            if path.is_dir():
                pending.append(path)
            else:
                size += path.stat().st_size
                require(size <= LIMITS['input_bytes'] and len(result) < 2048, 'code_snapshot_too_large')
                result[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result, omitted


def gitdir(root):
    marker = root / '.git'
    if marker.is_file():
        text = marker.read_text(encoding='utf-8').strip()
        require(text.startswith('gitdir: '), 'code_gitdir_invalid')
        own = (root / text[8:]).resolve()
    else:
        own = marker.resolve()
    common = (own / (own / 'commondir').read_text().strip()).resolve() if (own / 'commondir').exists() else own
    require(own.is_dir() and common.is_dir(), 'code_git_missing')
    return own, common


def revision_facts(root):
    own, common = gitdir(Path(root))
    paths = [own / 'HEAD', own / 'index', common / 'packed-refs']
    if (common / 'refs').exists():
        paths.extend(p for p in (common / 'refs').rglob('*') if p.is_file())
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None for p in paths}


class CodePayload:
    def __init__(self, root, script=None, baseline=None):
        from core.skills.code_change_git import configuration
        self.root = Path(root).resolve()
        require(self.root.is_dir(), 'code_workspace_missing')
        require(not check_credential_path(self.root), 'code_credential_workspace_refused')
        self.hashes, self.omitted = files(self.root)
        for rel in self.hashes:
            if Path(rel).name == '.gitattributes':
                require('filter' not in (self.root / rel).read_text(encoding='utf-8'), 'code_git_filters_unsupported')
        self.own, self.common = gitdir(self.root)
        self.revision = revision_facts(self.root)
        self.git_settings, self.git_config_hashes = configuration(self.common, self.own)
        self.command = script
        self.manifest = {'kind': 'code-validation', 'workspace': str(self.root), 'hashes': self.hashes,
                         'omitted': self.omitted, 'command': script, 'limits': dict(LIMITS),
                         'baseline_probe': baseline}
        self.script = '.\\python\\python.exe -I -B -X utf8 .\\code_change_worker.py; exit $LASTEXITCODE'
        self.approval_lines = ['显式只读隔离副本：' + str(self.root),
                               '保留共享checkout；不安装依赖、不联网、不自动提交。', script or 'Git只读基线/dirty/diff检查']

    def verify_unchanged(self):
        from core.skills.code_change_git import configuration
        require(files(self.root) == (self.hashes, self.omitted), 'code_workspace_changed')
        require(revision_facts(self.root) == self.revision, 'code_revision_changed')
        require(configuration(self.common, self.own) == (self.git_settings, self.git_config_hashes), 'code_git_config_changed')

    def stage(self, profile):
        import json

        from core.skills.code_change_git import runtime as git_runtime
        from core.skills.code_change_paths import attach

        self.verify_unchanged()
        source = self.root / next(iter(self.hashes))
        proxy = SimpleNamespace(path=source, before=self.hashes[source.relative_to(self.root).as_posix()],
                                kind='txt', manifest=self.manifest)
        DocumentPayload.stage(proxy, profile)
        repo = profile.work / 'repo'
        repo.mkdir()
        for rel in self.hashes:
            target = repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.root / rel, target)
            require(hashlib.sha256(target.read_bytes()).hexdigest() == self.hashes[rel], 'code_copy_drift')
        if self.command is None:
            copy_git(self, repo / '.git')
            _, binary = git_runtime()
            runtime = profile.work / 'git'
            runtime.mkdir()
            for path in binary.parent.iterdir():
                if path.name.lower() == 'git.exe' or path.suffix.lower() == '.dll':
                    shutil.copyfile(path, runtime / path.name)
        shutil.copyfile(Path(__file__).with_name('code_change_worker.py'), profile.work / 'code_change_worker.py')
        (profile.work / 'request.json').write_text(json.dumps({**self.manifest, 'sandbox_alias': attach(profile),
            'shell': str(profile.runtime / 'pwsh.exe')}), encoding='utf-8')
        self.verify_unchanged()


def copy_git(payload, dest):
    dest.mkdir()
    count = 0
    for name in ('HEAD', 'index', 'objects', 'refs', 'packed-refs'):
        src = (payload.own if name in {'HEAD', 'index'} else payload.common) / name
        if not src.exists():
            continue
        for path in ([src] if src.is_file() else src.rglob('*')):
            require(regular(path), 'code_git_reparse_refused')
            if path.is_file():
                require(path.name not in {'alternates', 'http-alternates'}, 'code_git_alternates_refused')
                count += path.stat().st_size
                require(count <= 128 * 1024 * 1024, 'code_git_metadata_too_large')
                target = dest / name if src.is_file() else dest / name / path.relative_to(src)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
    config = '[core]\nrepositoryformatversion = 0\nbare = false\n'
    config += ''.join(k + ' = ' + v + '\n' for k, v in payload.git_settings.items())
    (dest / 'config').write_text(config, encoding='utf-8')
