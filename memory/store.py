"""SQLite 会话存储（memory/store.py，基线 §4：唯一权威会话存储）。

设计约束：
- 自建版本化 schema（sessions/messages/runs/attempts/events/context_snapshots/memory_revisions），
  不耦合外部库结构；
- 单写入者、短事务：所有写操作经 threading.Lock 串行，网络等待期间不持有写事务；
- WAL 模式（辅助文件属正常现象，"单文件"指一个逻辑数据库）；
- 备份走 sqlite3 一致性备份接口，不复制 .db 文件；
- 请求快照绝不包含 API key（密钥只从环境变量读，不入库）。
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from core.ownership import Ownership
from memory.connection import open_connection
from memory.event_store import EventStoreMixin
from memory.migrations.schema import MEMORY_REVISIONS_DDL, SCHEMA_VERSION
from memory.repositories.artifacts import ArtifactRepository
from memory.repositories.checkpoints import CheckpointRepository
from memory.repositories.facts import FactRepository
from memory.repositories.history import HistoryRepository
from memory.repositories.operations import OperationRepository
from memory.repositories.runs import RunRepository
from memory.repositories.tasks import TaskRepository

__all__ = ["Store", "MEMORY_REVISIONS_DDL", "SCHEMA_VERSION"]


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Store(EventStoreMixin, RunRepository, ArtifactRepository, TaskRepository,
            OperationRepository, CheckpointRepository, HistoryRepository, FactRepository):
    """同步 SQLite 门面（写入经锁串行；读取可直接调）。"""

    def __init__(self, path: str | Path, *,
                 now: Callable[[], str] | None = None,
                 require_existing: bool = False) -> None:
        self._path = Path(path).resolve()
        self._conn = None
        self._owner = None
        # B02.02：时钟可注入（默认 UTC 实时钟；测试注入确定性时间戳源）
        self._now = now or _now
        if not require_existing:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        
        # 备份/收尾路径存在同线程再入）。必要性：WAL 只保证存储层并发，不防单连接上
        # 读写游标交错的 'recursive use of cursors'/'database error' 类错乱；
        
        self._lock = threading.RLock()
        self._owner = Ownership(self._path).acquire()
        try:
            self._conn = open_connection(self._path, self._now, require_existing=require_existing)
            from memory.forget_journal import replay
            replay(self)  # owner 已持有；恢复账本/历史/上下文之前先重放撤回。
            from memory.expiry import expire_contexts
            expire_contexts(self)
            with self.transaction():
                self._conn.execute("UPDATE operation_epoch SET generation=generation+1 WHERE id=1")
                self.fencing_generation = self._conn.execute("SELECT generation FROM operation_epoch").fetchone()[0]
                self._conn.execute("INSERT INTO runtime_owners VALUES(?,?,?,?,NULL)",
                                   (self._owner.owner_id, self._owner.generation,
                                    self._owner.pid, self._owner.record["started_at"]))
        except BaseException:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            self._owner.close()
            raise

    # ---- 会话 ------------------------------------------------------------------

    def create_session(self, session_id: str, *, mode: str = "queue",
                       config_version: str = "", agent_version: str = "") -> None:
        with self.transaction():
            self._conn.execute(
                "INSERT OR IGNORE INTO sessions(session_id, created_at, mode, config_version, agent_version)"
                " VALUES(?, ?, ?, ?, ?)",
                (session_id, self._now(), mode, config_version, agent_version),
            )

    def set_session_mode(self, session_id: str, mode: str) -> None:
        with self.transaction():
            self._conn.execute(
                "UPDATE sessions SET mode=? WHERE session_id=?", (mode, session_id),
            )

    def latest_session(self) -> str | None:
        with self._lock:   
            row = self._conn.execute(
                "SELECT session_id FROM sessions ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return row["session_id"] if row else None

    # ---- 消息 ------------------------------------------------------------------

    def append_message(self, message_id: str, session_id: str, *, role: str, content: str,
                       reasoning_content: str = "", status: str = "complete",
                       origin: str = "user", submit_seq: int | None = None,
                       run_id: str | None = None) -> None:
        """Upsert 语义：半成品行先写时，终态写入覆盖同 id 行（消息 id 唯一）。"""
        with self.transaction():
            self._conn.execute(
                "INSERT INTO messages(message_id, session_id, submit_seq, run_id, role, content,"
                " reasoning_content, status, origin, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(message_id) DO UPDATE SET content=excluded.content,"
                " reasoning_content=excluded.reasoning_content, status=excluded.status,"
                " run_id=COALESCE(excluded.run_id, messages.run_id)",
                (message_id, session_id, submit_seq, run_id, role, content,
                 reasoning_content, status, origin, self._now()),
            )

    def upsert_partial_assistant(self, *, message_id: str, run_id: str,
                                 content: str, reasoning_content: str) -> None:
        """流式半成品消息行（崩溃恢复用）；session_id 从 run 反查，run 缺失时跳过。"""
        with self.transaction():
            row = self._conn.execute(
                "SELECT session_id FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                return
            self._conn.execute(
                "INSERT INTO messages(message_id, session_id, submit_seq, run_id, role, content,"
                " reasoning_content, status, origin, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(message_id) DO UPDATE SET content=excluded.content,"
                " reasoning_content=excluded.reasoning_content, status=excluded.status",
                (message_id, row["session_id"], None, run_id, "assistant",
                 content, reasoning_content, "partial", "system", self._now()),
            )

    # ---- Run -------------------------------------------------------------------


    # ---- Attempt ---------------------------------------------------------------


    # ---- 事件 / 快照 -------------------------------------------------------------

    def save_context_snapshot(self, snapshot_id: str, run_id: str, record: dict) -> None:
        with self.transaction():
            self._conn.execute(
                "INSERT OR REPLACE INTO context_snapshots(snapshot_id, run_id, record_json, created_at)"
                " VALUES(?,?,?,?)",
                (snapshot_id, run_id, json.dumps(record, ensure_ascii=False), self._now()),
            )

    

    def save_compact_snapshot(self, snapshot_id: str, session_id: str, record: dict) -> None:
        """压缩快照落库（kind=compact_summary；生命周期字段在 record 内：pending /
        approved / dropped / rolled_back）。run_id 固存空串——Run 级快照恒有真 run_id，
        两种行互不干扰；零 schema 迁移（不改既有表结构语义，任务单边界③）。"""
        with self.transaction():
            self._conn.execute(
                "INSERT INTO context_snapshots(snapshot_id, run_id, record_json, created_at)"
                " VALUES(?,?,?,?)",
                (snapshot_id, "", json.dumps(record, ensure_ascii=False), self._now()),
            )

    def compact_snapshot_rows(self, session_id: str) -> list[dict]:
        with self._lock:   
            rows = self._conn.execute(
                "SELECT snapshot_id, record_json FROM context_snapshots"
                " WHERE run_id='' ORDER BY rowid DESC").fetchall()
            out = []
            for snapshot_id, record_json in rows:
                record = json.loads(record_json)
                if (record.get("kind") == "compact_summary"
                        and record.get("session_id") == session_id):
                    out.append({**record, "snapshot_id": snapshot_id})
        return out

    def latest_compact_snapshot(self, session_id: str,
                                 *, status: str | None = None) -> dict | None:
        """最新一条压缩快照（可按 status 过滤；无则 None）。"""
        with self._lock:   
            for row in self.compact_snapshot_rows(session_id):
                if status is None or row.get("status") == status:
                    return row
        return None

    def update_compact_snapshot_status(self, snapshot_id: str, status: str) -> bool:
        """Vastar runtime component."""
        with self.transaction():
            row = self._conn.execute(
                "SELECT record_json FROM context_snapshots WHERE snapshot_id=? AND run_id=''",
                (snapshot_id,)).fetchone()
            if row is None:
                logging.getLogger("vastar.store").warning(
                    "快照状态迁移落空：snapshot_id=%s 不存在（status=%s）——KM-12 显式信号",
                    snapshot_id, status)
                return False
            record = json.loads(row[0])
            record["status"] = status
            self._conn.execute(
                "UPDATE context_snapshots SET record_json=? WHERE snapshot_id=? AND run_id=''",
                (json.dumps(record, ensure_ascii=False), snapshot_id))
            return True

    # ---- 备份 -------------------------------------------------------------------

    def backup_to(self, dest_path: str | Path) -> None:
        """一致性在线备份（sqlite3 backup API），不复制 .db 文件。"""
        import sqlite3
        dest = sqlite3.connect(str(dest_path))
        try:
            with self._lock:
                self._conn.backup(dest)
        finally:
            dest.close()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    with self._conn:
                        self._conn.execute("UPDATE runtime_owners SET released_at=? WHERE owner_id=?",
                                           (self._now(), self._owner.owner_id))
                finally:
                    self._conn.close()
                    self._conn = None
            if self._owner is not None:
                self._owner.close()

    def __del__(self):
        # 兼容同步调用者遗忘 close；正常应用必须显式等待 writer 后再 close。
        if hasattr(self, "_lock"):
            try:
                self.close()
            except Exception:
                pass
