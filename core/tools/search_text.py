"""Vastar runtime component."""

from __future__ import annotations

import os
from pathlib import Path

from core.tools.discovery_scope import INCLUDE_GENERATED, DiscoveryScope
from core.tools.registry import ToolParamError, ToolSpec

NAME = "search_text"
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
_MAX_FILE_BYTES = 1024 * 1024   # 超过 1MB 的文本文件跳过并计数（防单文件拖垮）
_SNIPPET_CHARS = 200

DESCRIPTION = ("在文件内容中搜索关键词（先搜后读：返回 文件:行号:内容，行号 1-based，"
               "可直接喂 read_file 的 start_line 只读相关区间）。path 可限定目录或单文件；"
               "glob 过滤（如 *.py、**/*.py）；ignore_case 忽略大小写；结果上限"
               "默认 50（上限 200）。默认跳过生成目录，可用path或include_generated查归档；"
               ".git/.venv 与受保护路径始终跳过并计数。")
PARAMETERS = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "要搜索的关键词"},
        "path": {"type": "string", "description": "搜索根（目录或单文件，默认项目根）"},
        "glob": {"type": "string", "description": "文件名过滤模式（如 *.py；默认全部文件）"},
        "limit": {"type": "integer", "description": "最多返回条数（默认 50，上限 200）"},
        "ignore_case": {"type": "boolean", "description": "忽略大小写（默认 false）"},
        "include_generated": INCLUDE_GENERATED,
    },
    "required": ["query"],
}


def _iter_candidates(resolved_path: Path, pattern: str | None, skip=None):
    if resolved_path.is_file():
        yield resolved_path
        return
    if not resolved_path.is_dir():
        raise ToolParamError(f"路径不存在：{resolved_path}")
    pat = pattern or "*"
    while pat.startswith("**/"):        # rglob 本身即递归；去掉冗余前缀（兼容 **/*.py 写法）
        pat = pat[3:]
    if '/' in pat or '\\' in pat:
        from core.tools.glob_files import _matches
        notes = skip.notes if isinstance(skip, DiscoveryScope) else {
            'root': resolved_path, 'protected': set(), 'unreadable': {}}
        yield from _matches(resolved_path, pat, skip, notes)
        return
    # Prune before descending: filtering rglob results still walks .git/.venv trees.
    def scan_error(exc):
        if Path(exc.filename or resolved_path) == resolved_path:
            raise exc
        if isinstance(skip, DiscoveryScope):
            skip.notes['unreadable'][str(exc.filename)] = type(exc).__name__
    for base, directories, files in os.walk(resolved_path, onerror=scan_error):
        for name in directories:
            if skip is not None and skip(Path(base) / name):
                yield Path(base) / name  # preserve explicit protected-skip counts
        directories[:] = [name for name in directories if skip is None or not skip(Path(base) / name)]
        for name in files:
            entry = Path(base) / name
            if entry.match(pat):
                yield entry


def run(args: dict, *, resolved_path: Path, resolved_extra=None,
        project_root=None, skip=None, op_context=None) -> str:
    query = args.get("query")
    if not isinstance(query, str) or not query:
        raise ToolParamError("query 必须是非空字符串")
    limit = DEFAULT_LIMIT if args.get("limit") is None else int(args["limit"])
    if not 1 <= limit <= MAX_LIMIT:
        raise ToolParamError(f"limit 必须在 1~{MAX_LIMIT}（收到 {args.get('limit')}）")
    ignore_case = bool(args.get("ignore_case", False))
    needle = query.lower() if ignore_case else query
    scope = DiscoveryScope(args, resolved_path, project_root, skip)

    hits: list[str] = []
    scanned = skipped_binary = skipped_big = skipped_unreadable = 0
    from core.tools.operation_control import from_context
    control = from_context(op_context)
    for entry in _iter_candidates(resolved_path, args.get("glob"), scope):
        control.checkpoint('search_file')
        if len(hits) >= limit:
            break
        if scope(entry):
            continue
        if entry.is_dir():
            continue
        try:
            if entry.stat().st_size > _MAX_FILE_BYTES:
                skipped_big += 1
                continue
            with entry.open('rb') as stream:
                data = stream.read(_MAX_FILE_BYTES + 1)
        except PermissionError:
            skipped_unreadable += 1
            continue
        except OSError:
            skipped_unreadable += 1
            continue
        if len(data) > _MAX_FILE_BYTES:
            skipped_big += 1
            continue
        if b"\x00" in data[:1024]:
            skipped_binary += 1
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            skipped_binary += 1
            continue
        file_lines = text.split("\n")
        if file_lines and file_lines[-1] == "":
            file_lines.pop()             # 与 read_file 行口径一致：尾随换行不另计一行
        scanned += 1
        for lineno, line in enumerate(file_lines, start=1):
            control.checkpoint('search_line')
            hay = line.lower() if ignore_case else line
            if needle in hay:
                hits.append(f"{entry}: {lineno}: {line.strip()[:_SNIPPET_CHARS]}")
                if len(hits) >= limit:
                    break

    return _format_result(query, hits, limit, scanned, scope,
                          skipped_binary, skipped_big, skipped_unreadable)


def _format_result(query, hits, limit, scanned, scope, skipped_binary, skipped_big,
                   skipped_unreadable):
    notes = [f"命中 {len(hits)} 条（扫描 {scanned} 个文件，上限 {limit}）"]
    if scope.note():
        notes.append(scope.note())
    if scope.notes['protected']:
        notes.append(f"跳过保护路径 {len(scope.notes['protected'])} 个（.git/.venv/受保护文件名）")
    if scope.notes['unreadable']:
        notes.append(f"不可遍历子目录 {len(scope.notes['unreadable'])} 个，搜索范围不完整")
    if skipped_binary:
        notes.append(f"跳过二进制/非 UTF-8 文件 {skipped_binary} 个")
    if skipped_big:
        notes.append(f"跳过超 1MB 文件 {skipped_big} 个")
    if skipped_unreadable:
        notes.append(f"跳过无访问权限/不可读文件 {skipped_unreadable} 个")
    if len(hits) >= limit:
        notes.append(f"已达 limit={limit}，其余命中未列出——请缩小范围或提高 limit")
    body = "\n".join(hits) if hits else "（无命中）"
    return f"搜索 “{query}”：\n{body}\n（" + "；".join(notes) + "）"


SEARCH_TEXT_SPEC = ToolSpec(name=NAME, description=DESCRIPTION, parameters=PARAMETERS,
                            level="B", run=run, cooperative=True,
                            effect="read", resource_scope="tree", retryable=True,
                            cancel_phase="any", reconcile="reread")
