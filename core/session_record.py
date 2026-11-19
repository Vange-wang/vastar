"""Read a probe record from one SQLite snapshot of the designated session."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path


def run_watermark(path):
    if not Path(path).exists():
        return 0
    with closing(sqlite3.connect(path)) as db:
        return db.execute('SELECT coalesce(max(rowid),0) FROM runs').fetchone()[0]


def _anchor(db, run_id, watermark, first_input):
    if run_id:
        row = db.execute('SELECT session_id,rowid FROM runs WHERE run_id=?', (run_id,)).fetchone()
        if row:
            return row
        raise ValueError('Probe run not found')
    from core.harness.task_draft import DRAFT_INSTRUCTION, build_draft_text
    if not first_input:
        raise ValueError('Probe session requires its first input or an explicit run ID')
    goal = first_input.removeprefix('/task new ').strip()
    rows = db.execute(
        "SELECT DISTINCT r.session_id FROM runs r JOIN messages m ON m.message_id=r.input_message_id "
        "WHERE r.rowid>? AND m.role='user' AND (m.content IN (?,?) OR "
        "(substr(m.content,1,length(?))=? AND substr(m.content,-length(?))=?))",
        (watermark, first_input, build_draft_text(goal), DRAFT_INSTRUCTION, DRAFT_INSTRUCTION,
         '\n目标：' + goal, '\n目标：' + goal)).fetchall()
    if len(rows) != 1:
        raise ValueError('Cannot uniquely identify probe session from its first input')
    session_id = rows[0][0]
    return session_id, db.execute('SELECT max(rowid) FROM runs WHERE session_id=?', (session_id,)).fetchone()[0]


def session_record(path, *, run_id=None, watermark=0, first_input=None):
    """An explicit run anchors the replay at that point, even if later turns exist."""
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    try:
        db.execute('BEGIN')
        sid, last = _anchor(db, run_id, watermark, first_input)
        runs = [dict(row) for row in db.execute(
            'SELECT * FROM runs WHERE session_id=? AND rowid>? AND rowid<=? ORDER BY rowid',
            (sid, watermark, last))]
        selected = None
        stubs = []
        for run in reversed(runs):
            result = json.loads(run['result_json'] or '{}')
            mid = result.get('answer_message_id')
            answer = db.execute('SELECT content,status FROM messages WHERE session_id=? AND message_id=? '
                                "AND role='assistant'", (sid, mid)).fetchone()
            if answer and (answer['content'] or '').strip():
                if selected is None:
                    selected = run, dict(answer), mid
            else:
                stubs.append({'run_id': run['run_id'], 'terminal_state': run['terminal_state'],
                              'finish_reason': run.get('finish_reason') or result.get('finish_reason')})
        run, answer, mid = selected or (runs[-1], None, None)
        rid = run['run_id']
        # TASK-151③（HM-211）：events/attempts 合并本会话全部 run（runs 已按 rowid 升序，
        # 拼接即时间序）；每条事件带 run_id 保留 per-run 归属。事件天然唯一（event_id 主键、
        # 按 run_id 分区），跨会话不混入（run_ids 全部来自锚定会话）⇒ 不重复、不串会。
        # 旧口径（只取 anchor run）的盲区实证：TASK-150 C5-01-r1 record 0 件 vs 全会话 16 件。
        attempts = []
        events = []
        for r in runs:
            rr = r['run_id']
            attempts.extend(dict(a) for a in db.execute(
                'SELECT * FROM attempts WHERE run_id=? ORDER BY attempt_index', (rr,)))
            events.extend(dict(e) for e in db.execute(
                'SELECT run_id,type,payload FROM events WHERE run_id=? ORDER BY seq', (rr,)))
        assistant = db.execute("SELECT content,reasoning_content FROM messages WHERE session_id=? AND run_id=? "
                               "AND role='assistant' ORDER BY rowid DESC LIMIT 1", (sid, rid)).fetchone()
        return dict(run=run, answer=answer, answer_message_id=mid, events=events, attempts=attempts,
                    assistant=dict(assistant) if assistant else None, session_runs=runs, anchor_run_id=runs[-1]['run_id'],
                    stub_runs=stubs[::-1])
    finally:
        db.close()
