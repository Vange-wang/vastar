"""Owned Windows Job handles. Lifecycle only; NOT file/network isolation."""

import ctypes as C
import time
from ctypes import wintypes as W


def bind(lib, name, result, args):
    function = getattr(lib, name)
    function.restype, function.argtypes = result, args
    return function


class BasicLimits(C.Structure):
    _fields_ = [("process_time", C.c_int64), ("job_time", C.c_int64),
                ("flags", W.DWORD), ("min_ws", C.c_size_t), ("max_ws", C.c_size_t),
                ("active_limit", W.DWORD), ("affinity", C.c_size_t),
                ("priority", W.DWORD), ("scheduling", W.DWORD)]


class IOCounts(C.Structure):
    _fields_ = [(name, C.c_uint64) for name in
                ("reads", "writes", "others", "read_bytes", "write_bytes", "other_bytes")]


class ExtendedLimits(C.Structure):
    _fields_ = [("basic", BasicLimits), ("io", IOCounts),
                ("process_memory", C.c_size_t), ("job_memory", C.c_size_t),
                ("peak_process", C.c_size_t), ("peak_job", C.c_size_t)]


class WindowsJob:
    def __init__(self, memory_bytes=None):
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        create = bind(self.kernel, "CreateJobObjectW", W.HANDLE, [C.c_void_p, W.LPCWSTR])
        self.handle = create(None, None)
        if not self.handle:
            raise C.WinError(C.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE; no breakaway
        if memory_bytes is not None:
            if type(memory_bytes) is not int or memory_bytes < 64 * 1024 * 1024:
                self.close()
                raise ValueError("Job memory limit must be at least 64 MiB")
            limits.basic.flags |= 0x200  # JOB_MEMORY, aggregate committed memory
            limits.job_memory = memory_bytes
        setter = bind(self.kernel, "SetInformationJobObject", W.BOOL,
                      [W.HANDLE, C.c_int, C.c_void_p, W.DWORD])
        if not setter(self.handle, 9, C.byref(limits), C.sizeof(limits)):
            self.close()
            raise C.WinError(C.get_last_error())

    def assign(self, process):
        assign = bind(self.kernel, "AssignProcessToJobObject", W.BOOL, [W.HANDLE, W.HANDLE])
        if not assign(self.handle, process):
            raise C.WinError(C.get_last_error())

    def terminate(self):
        terminate = bind(self.kernel, "TerminateJobObject", W.BOOL, [W.HANDLE, W.UINT])
        if not terminate(self.handle, 125):
            raise C.WinError(C.get_last_error())
        deadline = time.monotonic() + 5
        while self.active_count():
            if time.monotonic() >= deadline:
                raise OSError("owned Job still has active processes; termination unconfirmed")
            time.sleep(0.01)

    def active_count(self):
        # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION: 4 LARGE_INTEGER, 4 DWORD.
        accounting = C.create_string_buffer(48)
        query = bind(self.kernel, "QueryInformationJobObject", W.BOOL,
                     [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.c_void_p])
        if not query(self.handle, 1, accounting, 48, None):
            raise C.WinError(C.get_last_error())
        return W.DWORD.from_buffer(accounting, 40).value

    def close(self):
        if self.handle:
            bind(self.kernel, "CloseHandle", W.BOOL, [W.HANDLE])(self.handle)
            self.handle = None
