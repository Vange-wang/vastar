"""Windows enforced exclusion for compare-and-mutate, including other processes.

No share-write/delete gap: compare and mutation use the same exclusive handle.
This provides concurrent-writer protection, not power-loss atomicity. A failed
write restores old bytes before releasing exclusion; integration keeps its journal.
API contracts: learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-createfilew
and nf-fileapi-setfileinformationbyhandle. Unsupported platforms fail closed.
"""
import ctypes
import os
from contextlib import contextmanager
from ctypes import wintypes as W


def _kernel():
    if os.name != 'nt':
        raise ValueError('workspace_enforced_publication_unavailable')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, W.LPVOID,
                                  W.DWORD, W.DWORD, W.HANDLE]
    kernel.CreateFileW.restype = W.HANDLE
    kernel.CloseHandle.argtypes = [W.HANDLE]
    kernel.CloseHandle.restype = W.BOOL
    kernel.SetFileInformationByHandle.argtypes = [W.HANDLE, ctypes.c_int, W.LPVOID, W.DWORD]
    kernel.SetFileInformationByHandle.restype = W.BOOL
    return kernel


def _open(kernel, path, access, sharing, creation, flags=0x80):
    handle = kernel.CreateFileW(str(path), access, sharing, None, creation, flags, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


@contextmanager
def _parents(kernel, root, target):
    handles = []
    try:
        handles.append(_open(kernel, root, 0, 3, 3, 0x02000000))
        current = root
        for part in target.parent.relative_to(root).parts:
            current /= part
            handles.append(_open(kernel, current, 0, 3, 3, 0x02000000))
        yield
    finally:
        for handle in reversed(handles):
            kernel.CloseHandle(handle)


def _delete(kernel, handle):
    # FILE_DISPOSITION_INFO.DeleteFile is BOOLEAN (one byte), class 4.
    delete = ctypes.c_ubyte(1)
    if not kernel.SetFileInformationByHandle(handle, 4, ctypes.byref(delete), ctypes.sizeof(delete)):
        raise ctypes.WinError(ctypes.get_last_error())


def _write(stream, value):
    stream.seek(0)
    offset = 0
    while offset < len(value):
        written = stream.write(value[offset:])
        if not written:
            raise OSError('workspace_publication_short_write')
        offset += written
    stream.truncate()
    stream.flush()
    os.fsync(stream.fileno())


def _mutate(kernel, handle, stream, expected, new):
    if new is None:
        _delete(kernel, handle)
        return
    try:
        _write(stream, new)
    except BaseException:
        if expected is None:
            _delete(kernel, handle)
        else:
            _write(stream, expected)
        raise


def compare_mutate(root, name, expected, new):
    from core.orchestration.workspace_io import safe_path

    kernel = _kernel()
    import msvcrt

    root = root.resolve()
    target = safe_path(root, name)
    target.parent.mkdir(parents=True, exist_ok=True)
    with _parents(kernel, root, target):
        safe_path(root, name)
        handle = _open(kernel, target, 0xC0010000, 0, 1 if expected is None else 3)
        try:
            safe_path(root, name)
            fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
        except BaseException:
            kernel.CloseHandle(handle)
            raise
        with os.fdopen(fd, 'r+b', buffering=0) as stream:
            actual = stream.read()
            if expected is not None and actual != expected:
                raise ValueError('workspace_expected_version_conflict:' + name)
            _mutate(kernel, handle, stream, expected, new)
