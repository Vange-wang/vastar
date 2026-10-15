"""list_dir：只读目录列举工具实现（Spec §4.2）。

权限级别判定**不在本文件**（由执行层统一收口）。
.git/ 与 .venv/ 一律跳过并**显式计数**（跳过数量进结果文本，禁静默）。
本文件内权限判定符号必须零命中（DoD 11 机械 grep）。
"""

from __future__ import annotations

from collections import deque
from pathlib import Path

from core.tools.registry import ToolParamError, ToolSpec

NAME = "list_dir"
DEFAULT_DEPTH = 1
MAX_DEPTH = 2
DEFAULT_LIMIT = 200
MAX_LIMIT = 200     
SKIP_DIRS = {".git", ".venv"}

DESCRIPTION = "列举项目内一个目录的内容（只读；.git/.venv 跳过并计数；depth≤2、limit≤200）。"
PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "相对项目根的目录路径（默认项目根）"},
        "depth": {"type": "integer", "description": "列举深度（默认 1，上限 2）"},
        "limit": {"type": "integer", "description": "最多列出的条目数（默认 200，上限 200）"},
    },
}


def run(args: dict, *, resolved_path: Path, resolved_extra=None,
        project_root=None, skip=None, op_context=None) -> str:
    """列举目录。resolved_path 由执行层完成解析后传入（默认 = 项目根）。"""
    depth = DEFAULT_DEPTH if args.get("depth") is None else int(args["depth"])
    limit = DEFAULT_LIMIT if args.get("limit") is None else int(args["limit"])
    if not 1 <= depth <= MAX_DEPTH:
        raise ToolParamError(f"depth 必须在 1~{MAX_DEPTH}（收到 {args.get('depth')}）")
    if not 1 <= limit <= MAX_LIMIT:
        raise ToolParamError(f"limit 必须在 1~{MAX_LIMIT}（收到 {args.get('limit')}）")

    lines: list[str] = []
    skipped = 0
    truncated = False
    stack: deque[tuple[Path, int]] = deque([(resolved_path, 1)])
    while stack:
        current, level = stack.popleft()
        try:
            entries = sorted(current.iterdir(), key=lambda p: (p.is_file(), p.name))
        except PermissionError:
            raise ToolParamError(
                f"无访问权限：{current}（该目录可能属于其他账户或受系统保护）") from None
        except FileNotFoundError:
            raise ToolParamError(f"路径不存在：{current}") from None
        except OSError as exc:
            raise ToolParamError(
                f"目录不可读：{current}（{type(exc).__name__}）——已显式失败，不静默") from exc
        for entry in entries:
            if entry.name in SKIP_DIRS:
                skipped += 1
                continue
            if len(lines) >= limit:
                truncated = True
                break
            if entry.is_dir():
                lines.append(f"{entry.name}/")
                if level < depth:
                    stack.append((entry, level + 1))
            else:
                lines.append(entry.name)
        if truncated:
            break

    out = "\n".join(lines) if lines else "（空目录）"
    if skipped:
        out += f"\n（跳过 .git/.venv 条目 {skipped} 个）"
    if truncated:
        out += f"\n（已达 limit={limit}，其余条目未列出）"
    return out


LIST_DIR_SPEC = ToolSpec(name=NAME, description=DESCRIPTION, parameters=PARAMETERS,
                         level="B", run=run, cooperative=True,
                         effect="read", resource_scope="tree", retryable=True,
                         cancel_phase="any", reconcile="reread")
