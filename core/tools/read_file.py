"""Vastar runtime component."""

from __future__ import annotations

from pathlib import Path

from core.tools.operation_control import from_context
from core.tools.registry import ToolParamError, ToolSpec

NAME = "read_file"
DEFAULT_MAX_BYTES = 16 * 1024
MAX_BYTES_CAP = 64 * 1024
DEFAULT_MAX_LINES = 200
MAX_LINES_CAP = 2000
_BINARY_SNIFF_BYTES = 1024
_SCAN_CHUNK = 64 * 1024          # 行索引扫描的单块上限（内存有界，禁全文缓冲）
_INDEX_CACHE_MAX = 8             # 进程内缓存文件数上限（单用户本地场景）

DESCRIPTION = ("读取文本文件，支持分段范围读：start_line（1-based，默认 1）+"
               " max_lines（默认 200，上限 2000）只读相关区间；max_bytes 是单次字节"
               "安全阀（默认 16384，上限 65536，与行数取先到）。返回带进度头/尾。"
               "硬约束：文件未读完前，不得对该文件下结论或作答；读大文件/日志时，"
               "先用 search_text 定位行号，再用 start_line/max_lines 只读相关区间。")
PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "相对项目根的路径，或绝对路径"},
        "start_line": {"type": "integer", "description": "从第几行开始（1-based，默认 1）"},
        "max_lines": {"type": "integer",
                      "description": "本次最多读多少行（默认 200，上限 2000）"},
        "max_bytes": {"type": "integer",
                      "description": "单次返回字节安全阀（默认 16384，上限 65536）"},
    },
    "required": ["path"],
}

# 行索引缓存：path -> (size, mtime_ns, total_lines, offsets)
# offsets[i] = 第 i+1 行起始字节偏移；末位恒为哨兵 size（供"本段末边界"取用）。
_LINE_INDEX_CACHE: dict[str, tuple[int, int, int, list[int]]] = {}


def line_offsets(data: bytes) -> list[int]:
    """各行起始字节偏移（"\\n" 分隔；尾随换行不另计一行，空文件为空表）——小文件路径专用。"""
    offsets: list[int] = [0] if data else []
    pos = 0
    while True:
        idx = data.find(b"\n", pos)
        if idx == -1:
            break
        pos = idx + 1
        if pos < len(data):
            offsets.append(pos)
    return offsets


def _open_rb(path: Path):
    """二进制句柄获取（间接层：测试以计数包装器挂钩统计 read() 字节量）。"""
    return path.open("rb")


def _scan_line_index(path: Path) -> tuple[int, int, list[int]]:
    """分块流式扫描构建行索引（单块 ≤_SCAN_CHUNK，任何时刻不持全文缓冲）。

    返回 (total_bytes, total_lines, offsets)；offsets 末位恒为哨兵 size。
    """
    size = path.stat().st_size
    offsets = [0]
    total_nl = 0
    pos = 0
    last_is_nl = False
    with _open_rb(path) as fh:
        while True:
            chunk = fh.read(_SCAN_CHUNK)
            if not chunk:
                break
            start = 0
            while True:
                idx = chunk.find(b"\n", start)
                if idx == -1:
                    break
                total_nl += 1
                offsets.append(pos + idx + 1)
                start = idx + 1
            pos += len(chunk)
            last_is_nl = chunk.endswith(b"\n")
    total_lines = total_nl if last_is_nl or size == 0 else total_nl + 1
    if offsets[-1] != size:
        offsets.append(size)          # 哨兵：末段边界
    return size, total_lines, offsets


def _line_index(path: Path) -> tuple[int, int, list[int]]:
    """带缓存的行索引（按 (size, mtime_ns) 失效；LRU 上限 _INDEX_CACHE_MAX）。"""
    st = path.stat()
    key = str(path)
    cached = _LINE_INDEX_CACHE.get(key)
    if cached and cached[0] == st.st_size and cached[1] == st.st_mtime_ns:
        _LINE_INDEX_CACHE.pop(key)
        _LINE_INDEX_CACHE[key] = cached       # 触碰移尾（简易 LRU）
        return cached[0], cached[2], cached[3]
    size, total_lines, offsets = _scan_line_index(path)
    if len(offsets) <= 400_000:               # 超大索引不缓存（每次现扫，防常驻膨胀）
        _LINE_INDEX_CACHE[key] = (size, st.st_mtime_ns, total_lines, offsets)
        while len(_LINE_INDEX_CACHE) > _INDEX_CACHE_MAX:
            _LINE_INDEX_CACHE.pop(next(iter(_LINE_INDEX_CACHE)))
    return size, total_lines, offsets


def _read_span(path: Path, offset: int, length: int) -> bytes:
    """Vastar runtime component."""
    with _open_rb(path) as fh:
        fh.seek(offset)
        return fh.read(length)


def _plan_hint(path: Path, total_lines: int, total_bytes: int) -> str:
    segments = max(1, (total_lines + DEFAULT_MAX_LINES - 1) // DEFAULT_MAX_LINES)
    starts = " / ".join(
        str(1 + i * DEFAULT_MAX_LINES) for i in range(min(3, segments)))
    return (f"{path} 共 {total_lines} 行 / {total_bytes} 字节，可分段读："
            f"start_line={starts} …（每段 max_lines={DEFAULT_MAX_LINES}）")


def _parse_args(args: dict) -> tuple[int, int, int]:
    start = 1 if args.get("start_line") is None else int(args["start_line"])
    max_lines = DEFAULT_MAX_LINES if args.get("max_lines") is None else int(args["max_lines"])
    budget = DEFAULT_MAX_BYTES if args.get("max_bytes") is None else int(args["max_bytes"])
    if start < 1:
        raise ToolParamError(f"start_line 必须 ≥ 1（1-based，收到 {args.get('start_line')}）")
    if not 1 <= max_lines <= MAX_LINES_CAP:
        raise ToolParamError(f"max_lines 必须在 1~{MAX_LINES_CAP}（收到 {args.get('max_lines')}）")
    if budget <= 0 or budget > MAX_BYTES_CAP:
        raise ToolParamError(f"max_bytes 必须在 1~{MAX_BYTES_CAP}（收到 {args.get('max_bytes')}）")
    return start, max_lines, budget


def run(args: dict, *, resolved_path: Path, resolved_extra=None,
        project_root=None, skip=None, op_context=None) -> str:
    """Vastar runtime component."""
    start, max_lines, budget = _parse_args(args)
    control = from_context(op_context)
    size = resolved_path.stat().st_size
    if b"\x00" in _read_span(resolved_path, 0, min(_BINARY_SNIFF_BYTES, size or 1))[:_BINARY_SNIFF_BYTES] \
            and size > 0:
        raise ToolParamError("疑似二进制文件（检测到 NUL 字节）——read_file 仅支持文本，显式拒绝")
    if size <= budget:
        # 现状行为（整文件 ≤ 本次预算 → 可整体读；禁用对象是"超预算大文件的一把读"）
        data = resolved_path.read_bytes()
        offsets = line_offsets(data)
        total_lines = len(offsets)
        total_bytes = len(data)
        if total_lines == 0:
            control.facts["read_window"] = {"start_line": 0, "end_line": 0, "total_lines": 0,
                                            "total_bytes": 0, "read_bytes": 0, "complete": True}
            return "（空文件，共 0 行 0 字节）"
        end = min(start - 1 + max_lines, total_lines)
        chunk = data[offsets[start - 1]:offsets[end] if end < total_lines else total_bytes]
        while len(chunk) > budget and end > start:
            end -= 1
            chunk = data[offsets[start - 1]:offsets[end] if end < total_lines else total_bytes]
        if len(chunk) > budget:
            raise ToolParamError(
                f"第 {start} 行单独达 {len(chunk)} 字节，超过字节安全阀 {budget}——显式拒绝；"
                "单行过大无法按行分段，建议用 search_text 精确定位关键词位置")
        control.facts["read_window"] = {"start_line": start, "end_line": end,
                                        "total_lines": total_lines, "total_bytes": total_bytes,
                                        "read_bytes": len(chunk), "complete": end >= total_lines}
        return _format(start, end, total_lines, total_bytes, len(chunk), chunk,
                       offsets[end] if end < total_lines else total_bytes)
    # 大文件路径：行索引（流式扫描 + 缓存）→ seek 定点读（任何时刻不持全文缓冲）
    total_bytes, total_lines, offsets = _line_index(resolved_path)
    if start > total_lines:
        raise ToolParamError(
            f"start_line={start} 超出总行数 {total_lines}——显式拒绝；"
            + _plan_hint(resolved_path, total_lines, total_bytes))
    end = min(start - 1 + max_lines, total_lines)
    while end > start and offsets[end] - offsets[start - 1] > budget:
        end -= 1                              # 字节阀整行回退（行边界对齐）
    if offsets[end] - offsets[start - 1] > budget:
        raise ToolParamError(
            f"第 {start} 行单独达 {offsets[end] - offsets[start - 1]} 字节，"
            f"超过字节安全阀 {budget}——显式拒绝；"
            "单行过大无法按行分段，建议用 search_text 精确定位关键词位置")
    chunk = _read_span(resolved_path, offsets[start - 1], offsets[end] - offsets[start - 1])
    if b"\x00" in chunk[:_BINARY_SNIFF_BYTES]:
        raise ToolParamError("疑似二进制文件（检测到 NUL 字节）——read_file 仅支持文本，显式拒绝")
    control.facts["read_window"] = {"start_line": start, "end_line": end,
                                    "total_lines": total_lines, "total_bytes": total_bytes,
                                    "read_bytes": len(chunk), "complete": end >= total_lines}
    return _format(start, end, total_lines, total_bytes, len(chunk), chunk, offsets[end])


def _format(start: int, end: int, total_lines: int, total_bytes: int,
            chunk_len: int, chunk: bytes, end_offset: int) -> str:
    """进度头/尾 + 正文（两策略共用，格式与 v1.9 验收一致）。"""
    try:
        text = chunk.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolParamError(f"非 UTF-8 文本（{exc}）——read_file 仅支持文本，显式拒绝") from exc
    head = (f"本次为第 {start}–{end} 行（共 {total_lines} 行）；"
            f"已读 {chunk_len} / 共 {total_bytes} 字节")
    if end < total_lines:
        remaining = total_bytes - end_offset
        tail = (f"尚有 {total_lines - end} 行（{remaining} 字节）未读；"
                f"如需继续，建议 start_line={end + 1}")
    else:
        tail = "已读完全部行"
    return f"{head}\n{text}\n{tail}"


READ_FILE_SPEC = ToolSpec(name=NAME, description=DESCRIPTION, parameters=PARAMETERS,
                          level="B", run=run, cooperative=True,
                          effect="read", resource_scope="file", retryable=True,
                          cancel_phase="any", reconcile="reread")
