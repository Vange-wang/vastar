"""Vastar runtime component."""
from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# 注意：core.tools.pagination 只能在函数内延迟导入——core.tools.__init__ 经
# registry → operation_receipt 回本模块，顶层导入会成环。
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.tools.pagination import PageResult

ARTIFACT_SCOPE = "tool_output"          # 本批唯一 scope（工具大输出）
DEFAULT_RETENTION = "30d"
PAGE_SIZE_DEFAULT = 16 * 1024
PAGE_SIZE_MAX = 64 * 1024
_BINARY_SNIFF = 8192


class ArtifactError(Exception):
    """产物库显式失败（data_dir 未初始化 / 落盘失败 / 索引失败）。"""


class ArtifactGone(ArtifactError):
    """引用显式失效（不存在 / 过期 / 损坏 / 资源已变化 / 越 owner·scope）。"""


@dataclass(frozen=True)
class ArtifactMeta:
    """产物元数据（任务单 A.1 十项齐 + owner + source_hash）。"""

    artifact_id: str
    scope: str
    uri: str
    hash: str
    size: int
    media_type: str
    source_ref: str
    created_at: str
    retention: str
    version: int
    owner: str = ""
    source_hash: str | None = None


def _media_type(content: bytes) -> str:
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return "application/octet-stream"
    return ("application/octet-stream" if b"\x00" in content[:_BINARY_SNIFF]
            else "text/plain; charset=utf-8")


def _file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


class ArtifactStore:
    """产物库门面：put（发布）/ get（读+校验）/ page（游标续读）/ delete（保留规则）/ sweep_orphans。"""

    def __init__(self, db, data_dir, *, owner: str, clock=None) -> None:
        self._db = db                       # Store（ArtifactRepository 混入）
        self._data_dir = Path(data_dir) if data_dir else None
        self._owner = owner                 # 项目身份（跨项目拒绝的判定基准）
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._checkpoint_refs = None        # B19 预留：callable(artifact_id, version) -> bool

    def set_checkpoint_ref_provider(self, provider) -> None:
        """B19 预留接口：注册"仍被活跃 checkpoint 引用"的判定源（本批只落规则不实现）。"""
        self._checkpoint_refs = provider

    # ---- A. 产物落盘 --------------------------------------------------------

    def available(self) -> bool:
        """Vastar runtime component."""
        return self._data_dir is not None

    def _require_dir(self) -> Path:
        if self._data_dir is None:
            raise ArtifactError("产物库不可用：data_dir 未初始化"
                                "（B13 口径：请显式 --init-project 或迁移，不静默退化）")
        return self._data_dir / "artifacts"

    def put(self, content: bytes, *, source_ref: str,
            retention: str = DEFAULT_RETENTION, scope: str = ARTIFACT_SCOPE) -> ArtifactMeta:
        # Process stdout/stderr publishers share version allocation with normal tools.
        # This is a mutex, not a long SQLite transaction; each index write stays short.
        with self._db._lock:
            return self._put(content, source_ref=source_ref, retention=retention, scope=scope)

    def _put(self, content: bytes, *, source_ref: str,
             retention: str, scope: str) -> ArtifactMeta:
        """临时 blob → 原子发布 → 事务索引；任一步失败都不留下"不可读取引用"。"""
        adir = self._require_dir()
        digest = hashlib.sha256(content).hexdigest()
        artifact_id = f"art-{digest[:16]}"
        existing = self._db.artifact_versions(artifact_id)
        version = max(existing) + 1 if existing else 1
        meta = ArtifactMeta(
            artifact_id=artifact_id, scope=scope,
            uri=f"artifact://{scope}/{artifact_id}/v{version}#{digest[:12]}",
            hash=digest, size=len(content), media_type=_media_type(content),
            source_ref=source_ref, created_at=self._clock().isoformat(),
            retention=retention, version=version, owner=self._owner,
            source_hash=_file_sha256(Path(source_ref)) if source_ref else None)
        adir.mkdir(parents=True, exist_ok=True)
        final = adir / f"{artifact_id}.v{version}.blob"
        tmp = adir / f"{final.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        try:
            with open(tmp, "wb") as fh:                     # ① 临时 blob
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, final)                          # ② 原子发布
        except OSError as exc:                              # 磁盘满/写失败：清临时文件
            tmp.unlink(missing_ok=True)
            raise ArtifactError(f"产物落盘失败：{exc}") from exc
        try:
            self._db.save_artifact(meta.__dict__)           # ③ 事务索引
        except Exception as exc:
            final.unlink(missing_ok=True)                   # 索引失败 → 撤 blob（无孤儿引用）
            raise ArtifactError(f"产物索引失败：{exc}") from exc
        return meta

    # ---- B. 引用读取与游标 ---------------------------------------------------

    def get(self, artifact_id: str, version: int) -> tuple[dict, bytes]:
        """读产物：索引缺失/blob 缺失/hash 不符/来源已变化 → 显式失效（ArtifactGone）。"""
        meta = self._read_meta(artifact_id, version)
        blob = self._blob_path(meta)
        if not blob.is_file():
            raise ArtifactGone(f"产物内容缺失（索引在、blob 不在）：{artifact_id} v{version}"
                               "——显式失效，请重新生成")
        data = blob.read_bytes()
        if hashlib.sha256(data).hexdigest() != meta["hash"]:
            raise ArtifactGone(f"产物内容损坏（hash 不符）：{artifact_id} v{version}"
                               "——显式失效，请重新生成")
        self._check_source_fresh(meta)
        return meta, data

    def page(self, artifact_id: str, version: int, *, cursor: str | None = None,
             page_size: int = PAGE_SIZE_DEFAULT) -> PageResult:
        """游标续读：cursor 校验 owner/scope/hash；资源已变化 → 显式失效。"""
        from core.artifact_paging import page_end
        from core.tools.pagination import PageResult, encode_cursor
        page_size = min(max(1, int(page_size)), PAGE_SIZE_MAX)
        meta, data = self.get(artifact_id, version)
        offset = self._check_cursor(cursor, meta) if cursor else 0
        try:
            end = page_end(data, offset, page_size,
                           text=meta["media_type"].startswith("text/"))
        except ValueError as exc:
            raise ArtifactGone(str(exc)) from exc
        chunk = data[offset:end]
        next_offset = offset + len(chunk)
        next_cursor = (encode_cursor(self._cursor_payload(meta, next_offset))
                       if next_offset < len(data) else None)
        return PageResult(items=(chunk,), next_cursor=next_cursor,
                          has_more=next_cursor is not None)

    def page_by_ref(self, ref: str, *, cursor: str | None = None,
                    page_size: int = PAGE_SIZE_DEFAULT) -> tuple[dict, PageResult, int]:
        """按 artifact_ref（uri 或裸 id）续读；返回 (meta, PageResult, 本次偏移)。"""
        artifact_id, version, scope = self._parse_ref(ref)
        digest = ref.strip().partition("#")[2] if ref.strip().startswith("artifact://") else None
        if scope != ARTIFACT_SCOPE:
            raise ArtifactGone(f"越 scope 拒绝：引用 scope={scope!r}（本库只服务 "
                               f"{ARTIFACT_SCOPE!r}）")
        meta = self._read_meta(artifact_id, version)
        if digest is not None and digest != meta["hash"][:12]:
            raise ArtifactGone("引用 hash 与产物不符——显式失效")
        offset = self._check_cursor(cursor, meta) if cursor else 0
        return meta, self.page(artifact_id, version, cursor=cursor,
                               page_size=page_size), offset

    # ---- C. 删除与孤儿清理 ----------------------------------------------------

    def delete(self, artifact_id: str, version: int) -> str:
        """保留/审计规则：保留期内/永久/checkpoint 引用 → 显式拒绝；允许则先删索引再删 blob。"""
        meta = self._db.get_artifact(artifact_id, version)
        if meta is None:
            return f"产物不存在：{artifact_id} v{version}"
        if meta["retention"] == "permanent":
            return f"拒绝删除：retention=permanent（长期保留）——{artifact_id} v{version}"
        if meta["retention"].endswith("d"):
            try:
                created = datetime.fromisoformat(meta["created_at"])
                left = (created.timestamp() + int(meta["retention"][:-1]) * 86400
                        - self._clock().timestamp())
            except ValueError:
                left = -1
            if left > 0:
                return (f"拒绝删除：保留期内（retention={meta['retention']}，"
                        f"剩余约 {int(left // 86400) + 1} 天）——{artifact_id} v{version}")
        if self._checkpoint_refs and self._checkpoint_refs(artifact_id, version):
            return ("拒绝删除：仍被活跃 checkpoint 引用（规则预留；checkpoint 实现属 B19）"
                    f"——{artifact_id} v{version}")
        self._db.delete_artifact_row(artifact_id, version)   # 先删索引
        self._blob_path(meta).unlink(missing_ok=True)        # 再删 blob（崩溃残留 = 孤儿，可 sweep）
        return f"已删除产物 {artifact_id} v{version}（审计：索引与内容均已移除）"

    def sweep_orphans(self) -> list[str]:
        """孤儿清理：无索引 blob + 临时残留 → 删除；返回清理名单（可审计）。"""
        adir = self._require_dir()
        if not adir.is_dir():
            return []
        known = self._db.artifact_blob_names()
        removed = []
        for p in sorted(adir.iterdir()):
            if p.name not in known:
                p.unlink(missing_ok=True)
                removed.append(p.name)
        return removed

    # ---- 内部 -----------------------------------------------------------------

    def _read_meta(self, artifact_id: str, version: int) -> dict:
        with self._db._lock:
            if self._db._conn.execute('SELECT 1 FROM memory_artifact_blocks WHERE artifact_id=? AND version=?',
                                      (artifact_id, version)).fetchone():
                raise ArtifactGone('记忆撤回已封锁派生产物；旧游标同样失效')
        meta = self._db.get_artifact(artifact_id, version)
        if meta is None:
            raise ArtifactGone(f"产物不存在或已删除：{artifact_id} v{version}")
        if meta["owner"] != self._owner:
            raise ArtifactGone("越 owner 拒绝：产物属其他项目/身份")
        if meta["scope"] != ARTIFACT_SCOPE:
            raise ArtifactGone("越 scope 拒绝：产物实际 scope 不属于工具输出")
        if meta.get('source_ref', '').startswith('browser:'):
            validator = getattr(self, 'browser_validator', None)
            if validator is None:
                raise ArtifactGone('浏览器会话不可用；请重新读取页面')
            validator(meta)
        return meta

    def _blob_path(self, meta: dict) -> Path:
        return self._require_dir() / f"{meta['artifact_id']}.v{meta['version']}.blob"

    def _cursor_payload(self, meta: dict, offset: int) -> dict:
        return {"artifact_id": meta["artifact_id"], "version": meta["version"],
                "offset": offset, "owner": self._owner, "scope": meta["scope"],
                "hash": meta["hash"]}

    def _check_cursor(self, cursor: str, meta: dict) -> int:
        from core.tools.pagination import decode_cursor
        try:
            payload = decode_cursor(cursor)
        except ValueError as exc:
            raise ArtifactGone(f"游标非法：{exc}") from exc
        if (payload.get("artifact_id"), payload.get("version")) != (
                meta["artifact_id"], meta["version"]):
            raise ArtifactGone("游标与引用不符（artifact_id/version 不匹配）——显式失效")
        if payload.get("owner") != self._owner:
            raise ArtifactGone("越 owner 拒绝：游标属其他项目/身份（跨项目取引用被拒）")
        if payload.get("scope") != meta["scope"]:
            raise ArtifactGone("越 scope 拒绝：游标 scope 与产物不符")
        if payload.get("hash") != meta["hash"]:
            raise ArtifactGone("游标 hash 与产物不符（资源已变化）——显式失效，请重新生成")
        offset = payload.get("offset")
        if type(offset) is not int or not 0 <= offset <= meta["size"]:
            raise ArtifactGone("游标偏移非法或超出产物范围")
        return offset

    def _check_source_fresh(self, meta: dict) -> None:
        source_hash = meta.get("source_hash")
        if not source_hash:
            return
        try:
            current = hashlib.sha256(Path(meta["source_ref"]).read_bytes()).hexdigest()
        except FileNotFoundError as exc:
            raise ArtifactGone("来源资源不存在——产物快照显式失效，请重新生成") from exc
        except OSError as exc:
            raise ArtifactGone("来源资源不可读——无法验证产物快照，显式失效") from exc
        if current != source_hash:
            raise ArtifactGone(f"来源资源已变化：{meta['source_ref']}——产物快照显式失效"
                               "（不得静默返回旧内容），请重新读取生成")

    @staticmethod
    def _parse_ref(ref: str) -> tuple[str, int, str]:
        """artifact://<scope>/<id>/v<N>#<hash> 或裸 <id>（缺省 v1/本批唯一 scope）。"""
        text = ref.strip()
        if not text.startswith("artifact://"):
            return text, 1, ARTIFACT_SCOPE
        body, sep, digest = text[len("artifact://"):].partition("#")
        parts = body.split("/")
        if len(parts) == 3 and parts[0] != ARTIFACT_SCOPE:
            raise ArtifactGone("越 scope 拒绝：引用不属于工具输出")
        if (len(parts) != 3 or not re.fullmatch(r"v[1-9][0-9]{0,17}", parts[2])
                or not sep or not re.fullmatch(r"[0-9a-f]{12}", digest)):
            raise ArtifactGone(f"引用形态非法：{ref!r}（期望 artifact://<scope>/<id>/v<N>#<hash>）")
        return parts[1], int(parts[2][1:]), parts[0]


def large_output_note(store: ArtifactStore | None, data: bytes, *, source_ref: str,
                      sensitive: bool) -> str:
    """截断标记段（B16·B.1）：显式标记 + artifact_ref + next_cursor；落不了产物也显式说明。

    短摘要 = 结果头部保留段（调用方负责）；本函数只产出尾部标记块。
    红线：sensitive（凭据类来源，R25）不落产物库；任何失败都不给出不可读取引用。
    """
    head = f"[大输出截断：共 {len(data)} 字节"
    if sensitive:
        return (head + "；来源属凭据类（R25）——不落产物库，"
                "请用范围读取（start_line/max_lines）分段获取]")
    if store is None:
        return head + "；产物库未装配（data_dir 未初始化）——无完整产物可续读]"
    try:
        meta = store.put(data, source_ref=source_ref)
    except ArtifactError as exc:
        return head + f"；产物落盘失败（{exc}）——无完整产物可续读]"
    from core.tools.pagination import encode_cursor
    cursor = encode_cursor({"artifact_id": meta.artifact_id, "version": meta.version,
                            "offset": 0, "owner": meta.owner, "scope": meta.scope,
                            "hash": meta.hash})
    return (head + f"；全量已落产物库]\n[大输出引用 artifact_id={meta.artifact_id} "
            f"version={meta.version} hash={meta.hash[:12]}… size={meta.size} "
            f"uri={meta.uri} retention={meta.retention}；续读：read_artifact "
            f"next_cursor={cursor}]")
