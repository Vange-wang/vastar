"""Runs ONLY inside the approved, read-only, offline ProcessService Job."""
import json
import os
import subprocess
from pathlib import Path


def git(root, *args):
    env = {**os.environ, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': 'NUL',
           'GIT_OPTIONAL_LOCKS': '0', 'GIT_TERMINAL_PROMPT': '0'}
    result = subprocess.run([str(Path(__file__).parent / 'git/git.exe'), '-c', 'safe.directory=*',
        '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=NUL', *args],
        cwd=root / 'repo', env=env, capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError('git_probe_failed:' + str(result.returncode) + ':' + result.stderr.decode('utf-8', errors='replace')[:2000])
    return result.stdout.decode('utf-8', errors='strict')


def inspect_git(root, request):
    flags = git(root, 'ls-files', '-v', '-z').split('\0')
    if any(item and not item.startswith('H ') for item in flags):
        raise RuntimeError('assume_unchanged_or_sparse_index_unsupported')
    # Omitted secrets/runtime directories must actually be ignored, not silently omitted source.
    tracked = git(root, 'ls-files', '-z').split('\0')
    for omitted in request['omitted']:
        if any(t == omitted.rstrip('/') or t.startswith(omitted) for t in tracked if t):
            raise RuntimeError('tracked_runtime_or_secret_unsupported')
        check = subprocess.run([str(Path(__file__).parent / 'git/git.exe'), '-c', 'safe.directory=*',
            'check-ignore', '--no-index', '--', omitted], cwd=root / 'repo', capture_output=True)
        if check.returncode:
            raise RuntimeError('unignored_omission_unsupported')
    head = git(root, 'rev-parse', '--verify', 'HEAD').strip()
    status = git(root, 'status', '--porcelain=v1', '-z', '--untracked-files=all', '--ignored=matching')
    diff = git(root, 'diff', '--no-ext-diff', '--no-textconv', 'HEAD', '--')
    return {'base_revision': head, 'dirty_raw': status, 'diff': diff}


def main():
    root = Path(__file__).resolve().parent
    request = json.loads((root / 'request.json').read_text(encoding='utf-8'))
    root = Path(request['sandbox_alias'])
    probe = inspect_git(root, request) if request['command'] is None else request['baseline_probe']
    print(json.dumps(probe), flush=True)
    if request['command'] is not None:
        shell = Path(request['shell'])
        command = "$ErrorActionPreference='Stop'; $LASTEXITCODE=0; " + request['command']
        command += '; if(-not $?) {exit 1}; exit $LASTEXITCODE'
        result = subprocess.run([str(shell), '-NoLogo', '-NoProfile', '-NonInteractive',
            '-Command', command], cwd=root / 'repo', check=False)
        return result.returncode
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print('S06_ENVIRONMENT:' + type(exc).__name__ + ':' + str(exc), flush=True)
        raise SystemExit(125) from None
