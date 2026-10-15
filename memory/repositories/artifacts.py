"""Vastar runtime component."""
from __future__ import annotations


class ArtifactRepository:
    """artifacts 表的增/查/删；blob 文件名推导与 DB 行一一对应（孤儿可核对）。"""

    def save_artifact(self, meta: dict) -> None:
        with self.transaction():
            self._conn.execute(
                "INSERT INTO artifacts(artifact_id, version, scope, uri, hash, size,"
                " media_type, source_ref, source_hash, owner, retention, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (meta["artifact_id"], meta["version"], meta["scope"], meta["uri"],
                 meta["hash"], meta["size"], meta["media_type"], meta["source_ref"],
                 meta.get("source_hash"), meta["owner"], meta["retention"],
                 meta["created_at"]))

    def get_artifact(self, artifact_id: str, version: int) -> dict | None:
        with self._lock:   
            row = self._conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id=? AND version=?",
                (artifact_id, version)).fetchone()
        return dict(row) if row else None

    def artifact_versions(self, artifact_id: str) -> list[int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT version FROM artifacts WHERE artifact_id=? ORDER BY version",
                (artifact_id,)).fetchall()
        return [r[0] for r in rows]

    def delete_artifact_row(self, artifact_id: str, version: int) -> None:
        with self.transaction():
            self._conn.execute(
                "DELETE FROM artifacts WHERE artifact_id=? AND version=?",
                (artifact_id, version))

    def artifact_blob_names(self) -> set[str]:
        """全部在册 blob 文件名（孤儿清理的对账基准）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT artifact_id, version FROM artifacts").fetchall()
        return {f"{aid}.v{ver}.blob" for aid, ver in rows}

    def artifacts_count(self, *, owner: str) -> int:
        """B40：本 owner 在册产物计数（/status 产物入口；只读）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM artifacts WHERE owner=?", (owner,)).fetchone()
        return row[0]

    def artifacts_page(self, *, owner: str, after: int = 0, limit: int = 50) -> list[dict]:
        """B40：产物元数据只读分页（rowid 键集次序：新增不挤动已翻页）；越 owner 不返回。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT rowid, artifact_id, version, scope, uri, hash, size, media_type,"
                " source_ref, retention, created_at FROM artifacts"
                " WHERE owner=? AND rowid>? ORDER BY rowid LIMIT ?",
                (owner, after, limit)).fetchall()
        return [dict(r) for r in rows]
