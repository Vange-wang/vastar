"""进程内协作取消与提交线性化；不构成 B18 持久账本。"""
import threading
import uuid
from contextlib import nullcontext


class OperationCancelled(Exception):
    """提交点之前明确停止；临时产物由工具 finally 清理。"""


class OperationControl:
    def __init__(self, request_id=""):
        self.operation_id = uuid.uuid4().hex
        self.request_id = request_id
        self.cancel_token = threading.Event()
        self._lock = threading.RLock()
        self.state = "preparing"
        self.history = ["preparing"]
        self.commit_started = False
        self.started = False
        self.verification = "not_needed"
        self.facts = {}
        self.reason = ""
        from core.budget_topup.tool_runtime import attach_control
        attach_control(self)

    def transition(self, state):
        with self._lock:
            self.state = state
            if not self.history or self.history[-1] != state:
                self.history.append(state)

    def request_cancel(self, reason="用户取消"):
        # 临界区仅修改状态，绝不持锁等待 I/O，避免阻塞事件循环。
        with self._lock:
            self.cancel_token.set()
            self.reason = reason
            if "cancel_requested" not in self.history:
                self.history.append("cancel_requested")
            if self.state not in ("committed", "failed", "cancelled_before_commit"):
                self.state = "cancel_requested"
        if getattr(self, "on_cancel", None):
            self.on_cancel()

    def checkpoint(self, stage="prepare"):
        with self._lock:
            if not self.commit_started and getattr(self, 'budget_check', None):
                self.budget_check()
            check = getattr(self, "authorization_check", None)
            if check is not None and not self.commit_started and not check():
                self.request_cancel("授权已撤回")
            if self.cancel_token.is_set() and not self.commit_started:
                self.transition("cancelled_before_commit")
                raise OperationCancelled(f"{self.reason}：提交前停止（{stage}）")

    def commit(self, action):
        """取消与进入不可撤回区二选一；系统调用途中不得谎称停止。"""
        self.checkpoint("before_commit")
        if getattr(self, "before_commit", None):
            self.before_commit()
        with getattr(self, "commit_fence", nullcontext)():
            with self._lock:
                self.checkpoint("before_commit")
                self.commit_started = True
            result = action()
            if getattr(self, "ledger", None):
                self.ledger.fault("after_file_commit")
            self.transition("committed")
            return result

    def receipt(self):
        with self._lock:
            return {"operation_id": self.operation_id, "operation_state": self.state,
                    "request_id": self.request_id,
                    "cancel_requested": self.cancel_token.is_set(),
                    "operation_history": list(self.history),
                    "verification": self.verification, **self.facts}


def from_context(op_context):
    return (op_context or {}).get("control") or OperationControl()
