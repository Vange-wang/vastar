"""Bounded anonymous pipes: only writers are inherited; parent never blocks on read."""
import ctypes as C
from ctypes import wintypes as W

from core.harness.windows_job import bind


class OutputPipes:
    def __init__(self, kernel, security):
        self.kernel, self.readers, self.writers = kernel, {}, []
        try:
            for stream in ("stdout", "stderr"):
                read, write = W.HANDLE(), W.HANDLE()
                if not bind(kernel, "CreatePipe", W.BOOL,
                            [C.POINTER(W.HANDLE), C.POINTER(W.HANDLE), C.c_void_p, W.DWORD])(
                                C.byref(read), C.byref(write), C.byref(security), 65536):
                    raise C.WinError(C.get_last_error())
                self.readers[stream] = read.value
                self.writers.append(write.value)
                if not bind(kernel, "SetHandleInformation", W.BOOL,
                            [W.HANDLE, W.DWORD, W.DWORD])(read, 1, 0):
                    raise C.WinError(C.get_last_error())
        except BaseException:
            self.close()
            raise

    def read(self, stream, limit=16384):
        available = W.DWORD()
        handle = self.readers[stream]
        peek = bind(self.kernel, "PeekNamedPipe", W.BOOL,
                    [W.HANDLE, C.c_void_p, W.DWORD, C.c_void_p, C.POINTER(W.DWORD), C.c_void_p])
        if not peek(handle, None, 0, None, C.byref(available), None):
            if C.get_last_error() == 109:
                return b""
            raise C.WinError(C.get_last_error())
        if not available.value:
            return b""
        data = C.create_string_buffer(min(limit, available.value))
        count = W.DWORD()
        if not bind(self.kernel, "ReadFile", W.BOOL,
                    [W.HANDLE, C.c_void_p, W.DWORD, C.POINTER(W.DWORD), C.c_void_p])(
                        handle, data, len(data), C.byref(count), None):
            raise C.WinError(C.get_last_error())
        return data.raw[:count.value]

    def release_writers(self):
        close = bind(self.kernel, "CloseHandle", W.BOOL, [W.HANDLE])
        for handle in self.writers:
            close(handle)
        self.writers.clear()

    def close(self):
        self.release_writers()
        close = bind(self.kernel, "CloseHandle", W.BOOL, [W.HANDLE])
        for handle in self.readers.values():
            close(handle)
        self.readers.clear()
