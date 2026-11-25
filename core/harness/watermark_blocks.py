"""blocks:v1 增量事件水位（TASK-174 / KM-27 方案 B）：分块派生索引，核验与检查点 save 同降 O(增量)。

派生表 event_wm_blocks（每 50000 行一块）随时可从 events 单方重算重建，不是一致性来源。
哈希公式（写死，测试对拍钉死）：
    H = sha256( json.dumps([各块 hash], ensure_ascii=False).encode() + b"\n" + 尾段文本 )
    尾段文本 = "(last_block_end, limit]" 行按 prefix:v1 同行构造（json.dumps([rowid,event_id,payload])，
    ", " 连接、"["/"]" 包裹）流式拼装；块摘要 = 块内行同构造文本的 sha256。
旧检查点无 "algo" 键 = prefix:v1，走 checkpoints.watermark 原路径（一字节不动）。
"""
import hashlib
import json
import time

_BLOCK_ROWS = 50000  # 块大小（=10×checkpoints._PAGE_SIZE）
_ALGO = "blocks:v1"


def _row_text(row):
    return json.dumps([row[0], row[1], row[2]], ensure_ascii=False)


def _hash_range(store, start_exclusive, end_inclusive, hasher):
    """把 (start, end] 的行按 prefix:v1 同构造文本流式写入 hasher（含 "["/"]" 包裹）。"""
    hasher.update(b"[")
    last, started = start_exclusive, False
    while True:
        sql = "SELECT rowid,event_id,payload FROM events WHERE rowid>?"
        params = [last]
        if end_inclusive is not None:
            sql += " AND rowid<=?"
            params.append(end_inclusive)
        with store._lock:
            page = store._conn.execute(sql + " ORDER BY rowid LIMIT 5000", params).fetchall()
        if not page:
            break
        for row in page:
            if started:
                hasher.update(b", ")
            hasher.update(_row_text(row).encode())
            started = True
        last = page[-1][0]
        if len(page) < 5000:
            break
    hasher.update(b"]")
    return last


def ensure_blocks(store, limit=None, budget_s=None):
    """按需补块（游标 = MAX(block_end)，自导出免 state_meta）；budget_s 给 hermes 式时间盒。

    返回 (新建块数, 是否已补齐到 limit)。块只封满块（50000 行），尾段不封。
    """
    t0 = time.perf_counter()
    built = 0
    while True:
        with store._lock:
            prev_end = store._conn.execute(
                "SELECT COALESCE(MAX(block_end),0) FROM event_wm_blocks").fetchone()[0]
            sql = "SELECT rowid,event_id,payload FROM events WHERE rowid>?"
            params = [prev_end]
            if limit is not None:
                sql += " AND rowid<=?"
                params.append(limit)
            rows = store._conn.execute(sql + " ORDER BY rowid LIMIT ?", (*params, _BLOCK_ROWS)).fetchall()
        if len(rows) < _BLOCK_ROWS:
            return built, True
        hasher = hashlib.sha256()
        hasher.update(b"[")
        hasher.update(", ".join(_row_text(r) for r in rows).encode())
        hasher.update(b"]")
        with store.transaction():
            store._conn.execute("INSERT OR REPLACE INTO event_wm_blocks VALUES(?,?,?)",
                                (rows[-1][0], len(rows), hasher.hexdigest()))
        built += 1
        if budget_s is not None and time.perf_counter() - t0 >= budget_s:
            return built, False


def watermark_blocks(store, limit=None):
    """blocks:v1 水位：块表（缺则先补）＋尾段流式；rowid/count 语义与 prefix:v1 一致。"""
    ensure_blocks(store, limit)
    with store._lock:
        sql = "SELECT block_end,hash FROM event_wm_blocks"
        params = []
        if limit is not None:
            sql += " WHERE block_end<=?"
            params.append(limit)
        blocks = store._conn.execute(sql + " ORDER BY block_end", params).fetchall()
        agg = store._conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(rowid),0) FROM events"
            + (" WHERE rowid<=?" if limit is not None else ""),
            (limit,) if limit is not None else ()).fetchone()
    hasher = hashlib.sha256()
    hasher.update(json.dumps([r[1] for r in blocks], ensure_ascii=False).encode())
    hasher.update(b"\n")
    last_end = blocks[-1][0] if blocks else 0
    _hash_range(store, last_end, limit, hasher)
    return {"algo": _ALGO, "rowid": agg[1] if agg[0] else 0,
            "count": agg[0], "hash": hasher.hexdigest()}
