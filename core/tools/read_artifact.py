"""Vastar runtime component."""
from __future__ import annotations

from core.artifacts import ArtifactError, ArtifactGone
from core.tools.registry import ToolParamError, ToolSpec


def _run(args: dict, *, resolved_path=None, resolved_extra=None, project_root=None,
         skip=None, op_context=None) -> str:
    store = (op_context or {}).get("artifacts")
    if store is None:
        raise ToolParamError("产物库未装配（data_dir 未初始化——B13 口径："
                             "先 --init-project 或迁移；不静默退化）")
    ref = str(args.get("artifact_ref", "")).strip()
    if not ref:
        raise ToolParamError("缺参数 artifact_ref（截断标记里的 artifact://… 引用）")
    cursor = args.get("cursor")
    page_size = int(args.get("page_size", 16 * 1024))
    try:
        meta, page, offset = store.page_by_ref(ref, cursor=cursor, page_size=page_size)
    except ArtifactGone as exc:
        return f"[大输出引用显式失效] {exc}"
    except ArtifactError as exc:
        raise ToolParamError(str(exc)) from exc
    chunk = page.items[0]
    if meta["media_type"] == "application/octet-stream":
        body = ("（二进制内容，hex 预览前 256 字节）\n"
                + chunk[:256].hex()
                + ("\n…（本页其余字节省略，续读请用 next_cursor）" if len(chunk) > 256 else ""))
    else:
        try:
            body = chunk.decode("utf-8")
        except UnicodeDecodeError:
            return "[大输出引用显式失效] 文本不是完整有效的 UTF-8，无法无损读取，请重新生成"
    end = offset + len(chunk)
    tail = (f"[续读 next_cursor={page.next_cursor}]" if page.has_more else "[末页：已读完全部内容]")
    if meta["media_type"] == "application/octet-stream" and not page.has_more:
        tail = "[末页：已到产物末尾；二进制仅展示各页预览，不代表完整内容]"
    return (f"[产物 {meta['artifact_id']} v{meta['version']} · 偏移 {offset}–{end}"
            f"/{meta['size']} 字节]\n{body}\n{tail}")


READ_ARTIFACT_SPEC = ToolSpec(
    name="read_artifact",
    description=("分页续读大输出产物：当工具结果带 [大输出引用 …] 截断标记时，"
                 "用 artifact_ref（必要时加 next_cursor）继续读取完整内容。"),
    parameters={"type": "object",
                "properties": {
                    "artifact_ref": {"type": "string",
                                     "description": "截断标记里的 artifact://… 引用（或裸 artifact_id）"},
                    "cursor": {"type": "string",
                               "description": "上一页返回的 next_cursor（首页可省略）"},
                    "page_size": {"type": "integer",
                                  "description": "每页字节数（默认 16384，上限 65536；至少读一个完整字符）"}},
                "required": ["artifact_ref"]},
    level="B", run=_run, preserve_full_result=True,
    effect="read", resource_scope="file", retryable=True,
    cancel_phase="any", reconcile="reread")
