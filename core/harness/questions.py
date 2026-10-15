"""T24 持久问题：不等待人、不授予权限、不把过期答案当同意。"""
import json
import time
import uuid

from memory.repositories.tasks import encode


class QuestionService:
    def __init__(self, tasks, clock=time.time):
        self.tasks, self.store, self.clock = tasks, tasks.store, clock

    def ask(self, session_id, *, question, options, required, expected_revision, timeout_s=3600):
        if not isinstance(question, str) or not question.strip():
            raise ValueError("问题不可空")
        if not isinstance(options, list) or any(not isinstance(v, str) for v in options):
            raise ValueError("options 必须为字符串数组")
        if type(required) is not bool or not 0 < timeout_s <= 604800:
            raise ValueError("required/timeout_s 无效")
        from core.harness.task_model import require_revision
        with self.store.transaction():
            task = self.tasks.current(session_id)
            require_revision(task["current_plan_revision"], expected_revision)
            if task["status"] in {"COMPLETED", "CANCELLED"}:
                raise ValueError("终态任务不可提问")
            qid = "question-" + uuid.uuid4().hex
            self.store._conn.execute("INSERT INTO task_questions VALUES(?,?,?,?,?,?,?,?,?,?)",
                                     (qid, task["task_id"], expected_revision, question, encode(options),
                                      int(required), self.clock() + timeout_s, "pending", "[]", self.store._now()))
            if required:
                self._state(task["task_id"], "WAITING_INPUT")
        return self.get(qid)

    def get(self, qid):
        with self.store._lock:
            row = self.store._conn.execute("SELECT * FROM task_questions WHERE question_id=?", (qid,)).fetchone()
            if row is None:
                raise ValueError("问题不存在")
            result = dict(row)
            result["options"], result["answers"] = json.loads(row["options"]), json.loads(row["answers"])
            return result

    def pending(self, task_id):
        with self.store.transaction():
            self.store._conn.execute("UPDATE task_questions SET state='expired' WHERE task_id=? "
                                     "AND state='pending' AND expires_at<=?", (task_id, self.clock()))
            return [self.get(r[0]) for r in self.store._conn.execute(
                "SELECT question_id FROM task_questions WHERE task_id=? AND state<>'answered'", (task_id,))]

    def answer(self, session_id, qid, answer, *, message_id):
        with self.store.transaction():
            task = self.tasks.current(session_id)
            q = self.get(qid)
            if q["task_id"] != task["task_id"]:
                raise ValueError("问题不属于当前 Task")
            source = self.tasks._source(session_id, message_id)
            accepted = (q["state"] == "pending" and self.clock() < q["expires_at"]
                        and q["plan_revision"] == task["current_plan_revision"]
                        and isinstance(answer, str) and bool(answer.strip()) and answer_matches(source["text"], qid, answer)
                        and (not q["options"] or answer in q["options"]))
            q["answers"].append({"answer": answer, "source": source, "accepted": accepted, "at": self.clock()})
            state = "answered" if accepted else ("expired" if self.clock() >= q["expires_at"] else q["state"])
            self.store._conn.execute("UPDATE task_questions SET state=?,answers=? WHERE question_id=?",
                                     (state, encode(q["answers"]), qid))
            unresolved = any(x["required"] for x in self.pending(task["task_id"]))
            if accepted and not unresolved:
                self._state(task["task_id"], "READY")
        return {"accepted": accepted, "question": self.get(qid)}

    def cancel(self, session_id, qid):
        with self.store.transaction():
            task, q = self.tasks.current(session_id), self.get(qid)
            if q["task_id"] != task["task_id"] or q["state"] == "answered":
                raise ValueError("问题不属于当前任务或已经回答")
            self.store._conn.execute("UPDATE task_questions SET state='cancelled' WHERE question_id=?", (qid,))
            self._state(task["task_id"], "WAITING_INPUT")
        return self.get(qid)

    def _state(self, task_id, status):
        self.store._conn.execute("UPDATE tasks SET status=?,revision=revision+1 WHERE task_id=?", (status, task_id))


def answer_matches(text, qid, answer):
    if text.strip() == answer:
        return True
    try:
        data = json.loads(text.split(" ", 1)[1]) if text.startswith("answer ") else {}
        return data.get("question_id") == qid and data.get("answer") == answer
    except (ValueError, IndexError, AttributeError):
        return False
