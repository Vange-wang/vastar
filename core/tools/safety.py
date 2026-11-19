"""写操作安全底线设施（TASK-24 / R25）：覆盖前备份 · 原子写 · 可逆删除落点。

底线四条中的三条在本模块实现（全量审计在执行层事件流）：
- 覆盖前自动备份：`backup_file()`——执行层在询问前调用；备份失败必须拒绝写（调用方捕获 OSError 转 DENY）；
- 原子写：`atomic_write_text()`——临时文件写满 + fsync + `os.replace`（同卷原子替换），
  中断不留半截文件（注入失败用例锁定）；
- 删除可逆：`recyclable()` / `recycle_delete()`（系统回收站，标准库 ctypes
  SHFileOperationW + FOF_ALLOWUNDO，零第三方依赖——红线 4）与 `move_to_trash()`
  （退化落点 `memory/_trash/<时间戳>-<短随机>/`，禁硬删）。

落点纪律（任务单 ①B 定死）：备份与 `_trash` 均在 `memory/` 之下（自动受硬拒清单覆盖）；
本模块的写入属**内部写入、不经权限层**——权限层只管模型发起的调用，故 `memory/**`
的 DENY 不会拦截备份与可逆删除本身（否则底线自缚）。唯一 id 用 uuid4
（Windows time_ns 粗分辨率坑，zcode.md §4）。
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from core.tools.operation_control import OperationControl

NO_RECHECK_NOTE = "（成功结果已含路径/字节/是否覆盖/备份位置/校验值——无需再调用只读工具复核）"


def verification_facts(path: Path) -> str:
    """写后校验事实（TASK-26 D2 / R25 补充⑪）：sha256 前 16 位 + mtime。

    文件读内容算摘要；目录 sha256 不适用（给 mtime），调用方在删除/移动**之前**取值
    可留"删除前/移动前"凭证。
    """
    mtime = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    if path.is_dir():
        return f"校验：目录（sha256 不适用）· mtime {mtime}"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    return f"校验：sha256 {digest} · mtime {mtime}"

_BACKUP_TOP = ("memory", "_backups")
_TRASH_TOP = ("memory", "_trash")

# B10（TASK-45）：删除结果的恢复凭证保留期限口径（如实声明，不虚构自动清理任务）
TRASH_RETENTION = "unlimited_no_auto_purge"   # memory/_trash 无自动清理——永久保留至手动清理
RECYCLE_RETENTION = "os_managed"              # 系统回收站由 OS 策略管理，Vastar 不控制
_DRIVE_FIXED = 3   # GetDriveTypeW：固定本地盘才有回收站；网络盘/可移动盘允许"永久删除"，必须排除

_FO_DELETE = 3
_FOF_ALLOWUNDO = 0x40
_FOF_SILENT = 0x04
_FOF_NOCONFIRMATION = 0x10
_FOF_NOERRORUI = 0x400

if os.name == "nt":

    class _SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", ctypes.c_void_p),
            ("wFunc", ctypes.c_uint),
            ("pFrom", ctypes.c_wchar_p),
            ("pTo", ctypes.c_wchar_p),
            ("fFlags", ctypes.c_ushort),
            ("fAnyOperationsAborted", ctypes.c_int),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", ctypes.c_wchar_p),
        ]


def _stamped_dir(base: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    return base / f"{stamp}-{uuid.uuid4().hex[:8]}"


def _relative_or_name(target: Path, project_root: Path) -> Path:
    """项目内保留相对结构（恢复即移回原位）；项目外按平名收纳。"""
    try:
        return target.resolve().relative_to(project_root.resolve())
    except ValueError:
        return Path(target.name)


def backup_file(src: Path, project_root: Path, *, data_dir: Path | None = None) -> Path:
    """把 src 复制进 memory/_backups/<时间戳>-<短随机>/（项目内保留相对结构）。

    返回备份文件路径；任何 I/O 失败原样抛 OSError——执行层必须据此拒绝写入。
    """
    base = data_dir / "memory/_backups" if data_dir else project_root.joinpath(*_BACKUP_TOP)
    dest = _stamped_dir(base) / _relative_or_name(src, project_root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return dest


@contextmanager
def prepared_parent(path, control):
    """准备目录可回退；只清理本次新建且仍为空的目录。"""
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    control.checkpoint("prepare")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        yield
    finally:
        for directory in missing:
            try:
                directory.rmdir()
            except OSError:
                pass  # 非空/已由外部移除：不动实际成果或他人内容。


def atomic_write_text(path: Path, text: str, *, control=None) -> int:
    """临时文件 + fsync；最终替换前检查取消，替换成功保留 committed。"""
    control = control or OperationControl()
    data = text.encode("utf-8")
    with prepared_parent(path, control):
        control.checkpoint("before_temp_write")
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".vastar-tmp-", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                for offset in range(0, len(data), 65536):
                    control.checkpoint("temp_write")
                    fh.write(data[offset:offset + 65536])
                fh.flush()
                os.fsync(fh.fileno())
            control.facts["expected"] = {str(path): {"exists": True,
                                                     "sha256": hashlib.sha256(data).hexdigest()}}
            control.commit(lambda: os.replace(tmp, path))
        finally:
            tmp.unlink(missing_ok=True)
    return len(data)


def dir_stats(root: Path) -> tuple[int, int]:
    """目录内条目数（含子目录）与文件字节总量（供批量阈值判定用的事实收集）。"""
    count = 0
    total = 0
    for entry in root.rglob("*"):
        count += 1
        if entry.is_file():
            total += entry.stat().st_size
    return count, total


def trash_dir(project_root: Path, *, data_dir: Path | None = None) -> Path:
    """本次可逆删除的落点目录：memory/_trash/<时间戳>-<短随机>/（每次删除独立目录）。"""
    return _stamped_dir(data_dir / "memory/_trash" if data_dir else
                        project_root.joinpath(*_TRASH_TOP))


def move_to_trash(target: Path, project_root: Path, *, data_dir: Path | None = None) -> Path:
    """把 target 移入本次 _trash 落点（项目内保留相对结构，项目外按平名收纳）。

    返回可恢复路径；同卷为原子改名，跨卷 shutil.move（复制成功后才删源——shutil 语义）。
    """
    dest = trash_dir(project_root, data_dir=data_dir) / _relative_or_name(target, project_root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(target), str(dest))
    return dest


def recovery_credential(recover, original: Path) -> dict:
    """删除结果的恢复凭证（B10/TASK-45）：恢复位置 + 原路径 + 保留期限（如实声明）。"""
    if recover == "系统回收站":
        return {"credential": "系统回收站", "original_path": str(original),
                "retention": RECYCLE_RETENTION}
    return {"credential": str(recover), "original_path": str(original),
            "retention": TRASH_RETENTION}


def recyclable(path: Path) -> bool:
    """系统回收站对该路径是否可用（Windows 固定盘）。非 Windows/非常规盘 → False。"""
    if os.name != "nt" or not path.is_absolute():
        return False
    try:
        drive = os.path.splitdrive(str(path))[0]
        if not drive:
            return False
        drive_type = int(ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(drive + "\\")))
    except Exception:  # noqa: BLE001 - 探测失败一律按"不可用"处理（走可逆退化路径）
        return False
    return drive_type == _DRIVE_FIXED


def recycle_delete(path: Path) -> bool:
    """把 path 移入系统回收站（SHFileOperationW + FOF_ALLOWUNDO）。

    成功返回 True；失败/中止/环境不支持返回 False（调用方退化到 move_to_trash）。
    """
    if os.name != "nt" or not path.exists():
        return False
    op = _SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = _FO_DELETE
    op.pFrom = str(path) + "\x00"   # 双 NUL 结尾（c_wchar_p 自动补第二个）
    op.pTo = None
    op.fFlags = _FOF_ALLOWUNDO | _FOF_SILENT | _FOF_NOCONFIRMATION | _FOF_NOERRORUI
    op.lpszProgressTitle = None
    try:
        ret = int(ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)))
    except Exception:  # noqa: BLE001 - 调用失败按"回收站不可用"处理
        return False
    return ret == 0 and not op.fAnyOperationsAborted
