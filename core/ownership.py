"""单实例/多入口归属：随机 owner、进程代次、心跳与内核锁；不使用 PID 存活猜测。"""
import json
import math
import os
import threading
import time
import uuid

from core.file_lock import FileLock

PROCESS_GENERATION = uuid.uuid4().hex
HEARTBEAT_TTL = 10.0


class OwnershipError(RuntimeError):
    pass


def atomic_json(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Ownership:
    def __init__(self, path, *, clock=time.time, interval=1.0):
        self.path = path.resolve()
        if self.path.exists() and self.path.stat().st_nlink > 1:
            raise OwnershipError("数据库存在硬链接；无法唯一锁定归属，拒绝启动")
        self.meta_path = self.path.with_name(self.path.name + ".owner.json")
        self.lock = FileLock(self.path.with_name(self.path.name + ".instance.lock"))
        self.owner_id = uuid.uuid4().hex
        self.generation = PROCESS_GENERATION
        self.pid = os.getpid()
        self.clock = clock
        self.interval = interval
        self.previous = None
        self.error = None
        self._stop = threading.Event()
        self._thread = None
        self.record = {}

    def acquire(self):
        try:
            self.lock.acquire()
        except OSError as exc:
            raise OwnershipError("数据库锁已被实例或迁移持有；不得抢占或中断其 Run") from exc
        try:
            self._check_previous()
            self.record = {"owner_id": self.owner_id, "generation": self.generation,
                           "pid": self.pid, "started_at": self.clock(),
                           "heartbeat": self.clock(), "state": "active", "database": str(self.path)}
            atomic_json(self.meta_path, self.record)
            self._thread = threading.Thread(target=self._heartbeat, daemon=True)
            self._thread.start()
            return self
        except BaseException:
            self.lock.close()
            raise

    def _check_previous(self):
        if not self.meta_path.exists():
            return  # 首次接入旧库；已持内核独占锁，未根据 Run/PID 推断。
        try:
            previous = json.loads(self.meta_path.read_text(encoding="utf-8"))
            age = self.clock() - float(previous["heartbeat"])
            if not (isinstance(previous["owner_id"], str) and previous["owner_id"]
                    and isinstance(previous["generation"], str) and previous["generation"]
                    and previous["state"] in ("active", "released") and math.isfinite(age)):
                raise ValueError("invalid ownership fields")
        except (ValueError, KeyError, TypeError) as exc:
            raise OwnershipError("所有权凭据损坏/路径不符；拒绝自动接管，请保留现场") from exc
        if previous["state"] == "active" and age <= HEARTBEAT_TTL:
            raise OwnershipError("锁已释放但 heartbeat 仍新鲜；须等心跳过期再核定接管")
        self.previous = previous

    def _heartbeat(self):
        while not self._stop.wait(self.interval):
            try:
                self.record["heartbeat"] = self.clock()
                atomic_json(self.meta_path, self.record)
            except BaseException as exc:
                self.error = exc
                return  # 保持内核锁；停止新的写事务，不让第二实例接管。

    def assert_held(self):
        if self.lock.handle is None or self.error is not None or self.pid != os.getpid():
            raise OwnershipError("本实例未持有有效数据库所有权；拒绝写入/恢复")

    def close(self):
        if self.lock.handle is None:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        try:
            if self.record:
                self.record.update(state="released", heartbeat=self.clock())
                atomic_json(self.meta_path, self.record)
        finally:
            self.lock.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.close()
