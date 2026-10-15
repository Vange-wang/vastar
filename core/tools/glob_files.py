"""Vastar runtime component."""

from __future__ import annotations

import os
from pathlib import Path

from core.tools.discovery_scope import INCLUDE_GENERATED, DiscoveryScope
from core.tools.registry import ToolParamError, ToolSpec

NAME = "glob_files"
DEFAULT_LIMIT = 100
MAX_LIMIT = 500


def _skip_path(path, skip, notes):
    if skip is not None and skip(path):
        if path not in getattr(skip, 'generated', ()):
            notes['protected'].add(str(path))
        return True
    return False


def _scan_error(path, exc, notes):
    if path == notes['root']:
        raise exc
    notes['unreadable'][str(path)] = type(exc).__name__


def _descendants(root, skip, notes):
    """Same recursive starting directories as rglob; isolate child scan failures."""
    yield root
    try:
        with os.scandir(root) as scanner:
            entries = list(scanner)
    except OSError as exc:
        _scan_error(root, exc, notes)
        return
    for entry in entries:
        path = root / entry.name
        if _skip_path(path, skip, notes):
            continue
        try:
            directory = entry.is_dir(follow_symlinks=False)
        except OSError as exc:
            _scan_error(path, exc, notes)
            continue
        if directory:
            yield from _descendants(path, skip, notes)


def _select(root, parts, skip, notes, directories_only):
    if not parts:
        yield root
        return
    if parts[0] == '**':
        for directory in _descendants(root, skip, notes):
            yield from _select(directory, parts[1:], skip, notes, directories_only)
        return
    # Delegate each nonrecursive component to pathlib itself (case, [], ?, ..).
    try:
        for path in root.glob(parts[0]):
            if _skip_path(path, skip, notes):
                continue
            if parts[1:] or directories_only:
                if not path.is_dir():
                    continue
            if parts[1:]:
                yield from _select(path, parts[1:], skip, notes, directories_only)
            else:
                yield path
    except OSError as exc:
        _scan_error(root, exc, notes)


def _matches(root, pattern, skip, notes):
    parsed = Path(pattern)
    if parsed.drive or parsed.root:
        raise NotImplementedError('Non-relative patterns are unsupported')
    if any('**' in part and part != '**' for part in parsed.parts):
        raise ValueError("Invalid pattern: '**' can only be an entire path component")
    directories_only = pattern.endswith(('/', os.sep))
    seen = set()
    for directory in _descendants(root, skip, notes):
        for path in _select(directory, parsed.parts, skip, notes, directories_only):
            if path not in seen:
                seen.add(path)
                yield path

DESCRIPTION = ("按文件名模式查找文件（支持 ** 递归，如 **/*.py、src/*.py）；结果上限"
               "默认 100（上限 500）；目录以 / 结尾标示。默认跳过生成目录，可用path或"
               "include_generated查归档；.git/.venv 与受保护路径始终跳过并计数。")
PARAMETERS = {
    "type": "object",
    "properties": {
        "pattern": {"type": "string", "description": "文件名模式（如 **/*.py、*.md）"},
        "path": {"type": "string", "description": "查找根目录（默认项目根）"},
        "limit": {"type": "integer", "description": "最多返回条数（默认 100，上限 500）"},
        "include_generated": INCLUDE_GENERATED,
    },
    "required": ["pattern"],
}


def run(args: dict, *, resolved_path: Path, resolved_extra=None,
        project_root=None, skip=None, op_context=None) -> str:
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise ToolParamError("pattern 必须是非空字符串（如 **/*.py）")
    limit = DEFAULT_LIMIT if args.get("limit") is None else int(args["limit"])
    if not 1 <= limit <= MAX_LIMIT:
        raise ToolParamError(f"limit 必须在 1~{MAX_LIMIT}（收到 {args.get('limit')}）")
    if not resolved_path.is_dir():
        raise ToolParamError(f"查找根必须是已存在的目录：{resolved_path}")
    pat = pattern
    while pat.startswith("**/"):        # rglob 本身即递归；去掉冗余前缀（兼容 **/*.py 写法）
        pat = pat[3:]

    found: list[str] = []
    scope = DiscoveryScope(args, resolved_path, project_root, skip)
    scan_notes = {'root': resolved_path, 'protected': set(), 'unreadable': {}}
    truncated = False
    for entry in _matches(resolved_path, pat, scope, scan_notes):
        if len(found) >= limit:
            truncated = True
            break
        found.append(f"{entry}/" if entry.is_dir() else str(entry))

    body = "\n".join(found) if found else "（无匹配文件）"
    notes = [f"匹配 {len(found)} 项（上限 {limit}）"]
    if scope.note():
        notes.append(scope.note())
    if scan_notes['protected']:
        notes.append(f"跳过保护路径 {len(scan_notes['protected'])} 个（.git/.venv/受保护文件名）")
    if scan_notes['unreadable']:
        detail = '；'.join(f'{path} [{kind}]' for path, kind in scan_notes['unreadable'].items())
        notes.append(f"不可遍历子目录 {len(scan_notes['unreadable'])} 个，查找范围不完整：{detail}")
    if truncated:
        notes.append(f"已达 limit={limit}，其余匹配未列出——请细化 pattern")
    return f"查找 “{pattern}”：\n{body}\n（" + "；".join(notes) + "）"


GLOB_FILES_SPEC = ToolSpec(name=NAME, description=DESCRIPTION, parameters=PARAMETERS,
                           level="B", run=run, cooperative=True,
                           effect="read", resource_scope="tree", retryable=True,
                           cancel_phase="any", reconcile="reread")
