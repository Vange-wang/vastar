"""显式长期记忆的审计写入（memory/audit.py，基线 §4 / 铁律 V3）。

阶段一口径：
- 每轮对话写的是"会话记录"（messages 表），不需要 LLM 再抽取；
- 长期记忆只支持显式操作且默认关闭（`long_term_enabled=false`），
  本模块提供显式写入/回滚的审计底座（/remember 等命令随后补齐时复用）；
- 每次变更记录 actor / origin / 时间 / 来源消息 / 操作 / 前后版本；
- 删除走可回滚的逻辑删除；
- origin 标注由调用方显式传入：正式入口只允许 user/system；
  测试触发的写入必须显式传 origin=test 且使用隔离数据库（铁律 V3）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from memory.store import MEMORY_REVISIONS_DDL
from memory.writer import Origin

_ALLOWED_ORIGINS = (Origin.USER, Origin.SYSTEM, Origin.TEST)


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class MemoryRevision:
    revision: int
    key: str
    value: str | None
    operation: str        # write / delete
    deleted: bool
    actor: str
    origin: str
    source_message_id: str | None
    previous_revision: int | None
    created_at: str


class MemoryAudit:
    """memory_revisions 表的审计读写（与 Store 同库；测试用独立文件路径隔离）。"""

    def __init__(self, conn: sqlite3.Connection, lock: object | None = None) -> None:
        self._conn = conn
        self._external_lock = lock

    # ---- 写入 ------------------------------------------------------------------

    def append_revision(self, *, key: str, value: str | None, operation: str,
                        actor: str, origin: str,
                        source_message_id: str | None = None) -> int:
        self._legacy_only()
        if origin not in _ALLOWED_ORIGINS:
            raise ValueError(f"非法 origin：{origin}（正式入口只能 user/system；测试必须显式 test）")
        prev = self._latest_revision(key)
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO memory_revisions(key, value, operation, deleted, actor, origin,"
                " source_message_id, previous_revision, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (key, value, operation, 1 if operation == "delete" else 0,
                 actor, origin, source_message_id, prev, _now()),
            )
            return int(cur.lastrowid)

    def rollback_revision(self, revision: int, *, actor: str, origin: str) -> int:
        """写一条补偿修订使 key 回到 revision 之前的状态（逻辑删除/恢复，物理数据不动）。"""
        self._legacy_only()
        target = self.get_revision(revision)
        if target is None:
            raise ValueError(f"修订不存在：{revision}")
        prev = self._latest_revision(target.key)
        if target.operation == "delete":
            restore_value = target.value
            restore_op = "write"
        else:
            
            prev_row = self._conn.execute(
                "SELECT value FROM memory_revisions"
                " WHERE key=? AND revision<? AND operation='write'"
                " ORDER BY revision DESC LIMIT 1",
                (target.key, revision)).fetchone()
            restore_value = prev_row["value"] if prev_row is not None else None
            restore_op = "write" if prev_row is not None else "delete"
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO memory_revisions(key, value, operation, deleted, actor, origin,"
                " source_message_id, previous_revision, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (target.key, restore_value, restore_op,
                 1 if restore_op == "delete" else 0, actor, origin,
                 target.source_message_id, prev, _now()),
            )
            return int(cur.lastrowid)

    # ---- 读取 ------------------------------------------------------------------

    def _latest_revision(self, key: str) -> int | None:
        row = self._conn.execute(
            "SELECT MAX(revision) AS r FROM memory_revisions WHERE key=?", (key,)
        ).fetchone()
        return int(row["r"]) if row and row["r"] is not None else None

    def get_revision(self, revision: int) -> MemoryRevision | None:
        row = self._conn.execute(
            "SELECT * FROM memory_revisions WHERE revision=?", (revision,)
        ).fetchone()
        return self._row_to_revision(row) if row else None

    def list_revisions(self, *, include_deleted: bool = True) -> list[MemoryRevision]:
        sql = "SELECT * FROM memory_revisions ORDER BY revision"
        rows = self._conn.execute(sql).fetchall()
        out = [self._row_to_revision(r) for r in rows]
        if include_deleted:
            return out
        return [r for r in out if not r.deleted]

    def effective_state(self) -> dict[str, str]:
        """按修订序列重放出当前生效内容（逻辑删除生效时不包含该 key）。"""
        self._legacy_only()
        state: dict[str, str] = {}
        for rev in self.list_revisions():
            if rev.deleted:
                state.pop(rev.key, None)
            elif rev.value is not None:
                state[rev.key] = rev.value
        return state

    def _legacy_only(self):
        if self._conn.execute("SELECT 1 FROM sqlite_master WHERE name='memory_items'").fetchone():
            raise ValueError('结构化记忆必须经 MemoryService；旧审计入口不可旁路写入/恢复')

    def record_commit(self, item, proposal_id, session_id):
        """唯一主链审计写入；由 MemoryService 外层事务控制，不在这里 commit。"""
        if not self._conn.in_transaction:
            raise ValueError('记忆修订须与事实及索引同事务')
        cur = self._conn.execute(
            'INSERT INTO memory_revisions(key,value,operation,deleted,actor,origin,source_message_id,'
            'previous_revision,created_at,proposal_id) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (item['id'], item['text'], 'write', 0, session_id, item['origin'],
             item['source_refs'][0], item['supersedes'], item['last_verified'], proposal_id))
        item['revision'] = cur.lastrowid
        self._conn.execute('UPDATE memory_revisions SET item_json=? WHERE revision=?',
                           (json.dumps(item, ensure_ascii=False), item['revision']))
        return item['revision']

    @staticmethod
    def _row_to_revision(row: sqlite3.Row) -> MemoryRevision:
        return MemoryRevision(
            revision=int(row["revision"]),
            key=row["key"],
            value=row["value"],
            operation=row["operation"],
            deleted=bool(row["deleted"]),
            actor=row["actor"],
            origin=row["origin"],
            source_message_id=row["source_message_id"],
            previous_revision=row["previous_revision"],
            created_at=row["created_at"],
        )


def open_memory_db(path: str | Path) -> tuple[sqlite3.Connection, MemoryAudit]:
    """打开（或创建）记忆库并确保 schema 存在；测试用 tmp_path 调用即天然隔离。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    with conn:
        conn.execute(MEMORY_REVISIONS_DDL)
    return conn, MemoryAudit(conn)


def revisions_to_json(revisions: list[MemoryRevision]) -> str:
    return json.dumps([r.__dict__ for r in revisions], ensure_ascii=False, indent=2)
