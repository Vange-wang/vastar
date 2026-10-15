"""本机数据库生命周期锁；锁文件永久保留，避免删除重建导致双锁。"""
import os


class FileLock:
    def __init__(self, path):
        self.path = path
        self.handle = None

    def acquire(self):
        handle = self.path.open("a+b")
        try:
            if self.path.stat().st_size == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            handle.close()
            raise
        self.handle = handle

    def close(self):
        if self.handle is not None:
            self.handle.close()  # 内核释放锁；进程死亡也自动释放。
            self.handle = None
