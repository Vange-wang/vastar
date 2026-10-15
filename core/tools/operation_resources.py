"""进程内资源占用；共享到不同 executor，父子路径互斥。"""
import hashlib
import os
import threading
from pathlib import Path

_LOCK = threading.RLock()
_OWNERS = {}


def claim(owner, paths, *, read_only=False):
    keys = {Path(os.path.normcase(str(Path(p).resolve()))) for p in paths}
    # 还不存在的父目录也会被创建，纳入资源集合防止准备期互相抢占。
    for path in list(keys):
        parent = path.parent
        while not parent.exists():
            keys.add(parent)
            parent = parent.parent
    with _LOCK:
        if owner in _OWNERS:
            return False  # 同 ID 即使指向不同路径也不能并发覆盖执行体身份。
        for occupied, shared in _OWNERS.values():
            if read_only and shared:
                continue
            if any(a == b or a in b.parents or b in a.parents for a in keys for b in occupied):
                return False
        _OWNERS[owner] = (keys, read_only)
    return True


def release(owner):
    with _LOCK:
        _OWNERS.pop(owner, None)


def file_facts(paths):
    """仅在权限放行后采集声明资源的结果；核实失败也显式保留。"""
    facts = {}
    for path in paths:
        path = Path(path)
        try:
            if not path.exists():
                facts[str(path)] = {"exists": False}
            elif path.is_file():
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(65536), b""):
                        digest.update(block)
                facts[str(path)] = {"exists": True, "sha256": digest.hexdigest()}
            else:
                facts[str(path)] = {"exists": True, "kind": "directory",
                                    "entries": sorted(p.name for p in path.iterdir())}
        except OSError as exc:
            facts[str(path)] = {"verification_error": type(exc).__name__}
    return facts


def proves_unchanged(before, after):
    """只有完整的文件/缺失事实可证明无变化；目录名称列表不覆盖子项内容。"""
    return bool(before and before == after and all(
        "verification_error" not in item and item.get("kind") != "directory"
        for item in before.values()))
