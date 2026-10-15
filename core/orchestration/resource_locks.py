"""Cross-process exclusive resource ownership; no lease expiry can steal live writes."""
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path

from core.file_lock import FileLock


class ResourceLock:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def hold(self, resource_key, owner):
        if not isinstance(resource_key, str) or not resource_key.strip() or not owner:
            raise ValueError('resource_identity_required')
        path = self.directory / (sha256(resource_key.encode()).hexdigest() + '.lock')
        lock = FileLock(path)
        try:
            lock.acquire()
        except OSError as exc:
            raise ValueError('resource_busy:' + resource_key) from exc
        try:
            yield
        finally:
            lock.close()
