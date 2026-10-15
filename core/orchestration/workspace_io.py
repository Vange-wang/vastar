"""Conservative source snapshots and hash-checked replacements for B47 directories."""
import base64
import hashlib
import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

EXCLUDED = frozenset({'.git', '.work', '.venv', '__pycache__', '.pytest_cache', '.ruff_cache'})


def safe_path(root, name):
    from core.tools.permissions import check_credential_path, is_env_path

    path = Path(name)
    if path.is_absolute() or not path.parts or '..' in path.parts or ':' in name:
        raise ValueError('workspace_invalid_path')
    target = root / path
    if (any(part in EXCLUDED for part in path.parts) or is_env_path(target)
            or check_credential_path(target) is not None):
        raise ValueError('workspace_protected_path')
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError('workspace_path_escape')
    for parent in (target, *target.parents):
        if parent == root:
            break
        if parent.is_symlink() or (hasattr(parent, 'is_junction') and parent.is_junction()):
            raise ValueError('workspace_link_refused')
    return target


def snapshot(root):
    from core.tools.permissions import check_credential_path, is_env_path

    result = {}
    if (root / '.git').exists():
        names = subprocess.check_output(['git', '-C', str(root), 'ls-files', '-z',
                                         '--cached', '--others', '--exclude-standard'],
                                        encoding='utf-8').split('\0')
        for name in sorted(set(names) - {''}):
            if (is_env_path(root / name) or check_credential_path(root / name) is not None
                    or any(p in EXCLUDED for p in Path(name).parts)):
                continue
            path = safe_path(root, name)
            if path.is_file():
                result[name] = path.read_bytes()
        return result
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED)
        for name in dirs + sorted(f for f in files if f not in EXCLUDED):
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if is_env_path(path) or check_credential_path(path) is not None:
                if name in dirs:
                    dirs.remove(name)
                continue
            safe_path(root, relative)
            if path.is_file():
                result[relative] = path.read_bytes()
    return result


def digest(data):
    return hashlib.sha256(data).hexdigest()


def hashes(files):
    return {name: digest(data) for name, data in sorted(files.items())}


def revision(root, files):
    if (root / '.git').exists():
        return subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'],
                                       text=True, encoding='utf-8').strip()
    return 'sha256:' + digest(json.dumps(hashes(files), sort_keys=True).encode())


def dirty(root):
    if not (root / '.git').exists():
        return None
    return subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain=v1', '-z'],
                                   encoding='utf-8')


def pack(files):
    return {key: base64.b64encode(value).decode('ascii') for key, value in files.items()}


def unpack(files):
    return {key: base64.b64decode(value, validate=True) for key, value in files.items()}


def save(path, value):
    temp = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding='utf-8')
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def materialize(root, files):
    root.mkdir(parents=True, exist_ok=False)
    for name, data in files.items():
        target = safe_path(root, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def replace_file(root, name, expected, new):
    from core.orchestration.workspace_atomic import compare_mutate

    compare_mutate(root, name, expected, new)
