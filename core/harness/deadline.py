"""墙钟兜底原语（TASK-168；hermes `run_bounded_sync` 同形状，`agent/deadline.py` 出处）。

超时不杀 worker（CPython 无法安全杀线程），daemon 线程直接遗弃；
`on_timeout` 在调用方线程 best-effort 执行（杀进程树等善后的唯一挂点）。
**低频路径专用**：每次超时泄漏一个 daemon 线程，禁热循环 per-item 使用。
"""
import threading
import time
from dataclasses import dataclass

_WAIT_SLICE_S = 0.2   # 分片等待：/stop、SIGINT 200ms 内可观测


@dataclass
class BoundedResult:
    timed_out: bool
    value: object
    elapsed_s: float
    timeout_s: float | None
    label: str


def run_bounded_sync(fn, timeout, *, label="operation", on_timeout=None):
    """在 daemon worker 线程跑 `fn`；`timeout` 秒未完成则遗弃 worker 并返回 `timed_out=True`。

    `fn` 抛出的异常在调用方线程原样重抛；`on_timeout` 在调用方线程 best-effort
    执行（异常吞掉——善后失败不得二次伤害调用方）。`timeout=None/<=0` → 同步直通不起线程。
    """
    if timeout is None or timeout <= 0:
        return BoundedResult(False, fn(), 0.0, timeout, label)
    done = threading.Event()
    box = {}

    def work():
        try:
            box["value"] = fn()
        except BaseException as exc:  # 原样转运到调用方线程
            box["error"] = exc
        finally:
            done.set()

    threading.Thread(target=work, name=f"deadline-{label}", daemon=True).start()
    start = time.monotonic()
    while True:
        remaining = timeout - (time.monotonic() - start)
        if done.wait(min(_WAIT_SLICE_S, max(remaining, 0.0))):
            break
        if time.monotonic() - start >= timeout:
            if on_timeout is not None:
                try:
                    on_timeout()
                except Exception:
                    pass    # best-effort 善后，异常不外抛（hermes 同口径）
            return BoundedResult(True, None, time.monotonic() - start, timeout, label)
    if "error" in box:
        raise box["error"]
    return BoundedResult(False, box.get("value"), time.monotonic() - start, timeout, label)
