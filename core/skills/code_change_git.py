"""Only inert core Git settings enter the private copy; no helpers, credentials or includes."""
import hashlib
import re
import shutil
from pathlib import Path

from core.skills.code_change_snapshot import require

ALLOWED = {'autocrlf': {'true', 'false', 'input'}, 'eol': {'lf', 'crlf', 'native'},
           'filemode': {'true', 'false'}, 'ignorecase': {'true', 'false'}, 'symlinks': {'true', 'false'},
           'trustctime': {'true', 'false'}}


def runtime():
    selected = shutil.which('git')
    require(selected, 'code_git_runtime_unavailable')
    entry = Path(selected).resolve()
    require(entry.is_file(), 'code_git_runtime_unavailable')
    root = entry.parent.parent
    if entry.parent.name.lower() == 'bin' and root.name.lower() == 'mingw64':
        root = root.parent
    binary = root / 'mingw64/bin/git.exe'
    require(binary.is_file(), 'code_git_runtime_unavailable')
    return root, binary


def configuration(common, own):
    git_root, _ = runtime()
    paths = [git_root / 'etc/gitconfig', Path.home() / '.config/git/config', Path.home() / '.gitconfig',
             common / 'config', own / 'config.worktree']
    values, hashes = {}, {}
    for path in paths:
        if not path.exists():
            hashes[str(path)] = None
            continue
        content = path.read_bytes()
        hashes[str(path)] = hashlib.sha256(content).hexdigest()
        section = ''
        for line in content.decode('utf-8-sig').splitlines():
            line = line.strip()
            if line.startswith('['):
                section = line.lower()
                require(not section.startswith('[include'), 'code_git_custom_config_unsupported')
            elif section == '[core]' and '=' in line:
                key, value = (s.strip().lower() for s in line.split('=', 1))
                if key in ALLOWED:
                    value = re.split(r'\s*[#;]', value)[0].strip('"')
                    require(value in ALLOWED[key], 'code_git_core_setting_unsupported')
                    values[key] = value
    return values, hashes
