"""Private AppContainer profile: no capability SIDs and no host ACL changes."""

import ctypes as C
import shutil
import time
import uuid
from ctypes import wintypes as W
from pathlib import Path

from core.harness.windows_job import bind


class ContainerProfile:
    def __init__(self):
        self.name = "Vastar.Execution." + uuid.uuid4().hex
        self.userenv = C.WinDLL("userenv", use_last_error=True)
        self.sid = C.c_void_p()
        create = bind(self.userenv, "CreateAppContainerProfile", W.LONG,
                      [W.LPCWSTR, W.LPCWSTR, W.LPCWSTR, C.c_void_p, W.DWORD,
                       C.POINTER(C.c_void_p)])
        result = create(self.name, self.name, "Vastar offline execution", None, 0,
                        C.byref(self.sid))
        if result:
            raise OSError(f"AppContainer creation failed: HRESULT {result}")
        self.root = self._folder()
        self.work = self.root / "work"
        self.runtime = self.root / "runtime"
        self.work.mkdir()
        (self.root / "Temp").mkdir(exist_ok=True)

    def _folder(self):
        text = W.LPWSTR()
        convert = bind(C.WinDLL("advapi32"), "ConvertSidToStringSidW", W.BOOL,
                       [C.c_void_p, C.POINTER(W.LPWSTR)])
        if not convert(self.sid, C.byref(text)):
            raise C.WinError(C.get_last_error())
        folder = W.LPWSTR()
        get = bind(self.userenv, "GetAppContainerFolderPath", W.LONG,
                   [W.LPCWSTR, C.POINTER(W.LPWSTR)])
        try:
            if get(text.value, C.byref(folder)):
                raise OSError("AppContainer folder unavailable")
            root = Path(folder.value).resolve()
            if root.parent.name.casefold() != self.name.casefold():
                raise OSError("AppContainer folder ownership mismatch")
            return root
        finally:
            bind(C.WinDLL("kernel32"), "LocalFree", C.c_void_p, [C.c_void_p])(text)
            if folder:
                bind(C.WinDLL("ole32"), "CoTaskMemFree", None, [C.c_void_p])(folder)

    def install_runtime(self):
        executable = shutil.which("pwsh")
        if not executable:
            raise OSError("PowerShell 7 required; no Windows PowerShell fallback")
        shutil.copytree(Path(executable).resolve().parent, self.runtime)

    def close(self):
        # Delete only the OS profile created by this instance; no caller-supplied path.
        from core.harness.windows_readonly import reject_reparse_points

        package = self.root.parent
        if package.resolve() != package or package.name.casefold() != self.name.casefold():
            raise PermissionError("profile ownership mismatch; refusing cleanup")
        if package.exists():
            reject_reparse_points(package)
        delete = bind(self.userenv, "DeleteAppContainerProfile", W.LONG, [W.LPCWSTR])
        # Job active_count=0 does not guarantee all filesystem image/AV handles
        # have been released. DeleteAppContainerProfile may unregister a profile
        # before finishing file deletion, so success alone is not cleanup proof.
        failure = None
        for delay in (0, 0.05, 0.1, 0.2, 0.4, 0.8, 1.0):
            if delay:
                time.sleep(delay)
            result = delete(self.name)
            if result:
                failure = OSError(f"AppContainer cleanup failed: HRESULT {result}")
                if (result & 0xFFFFFFFF) not in (0x80070020, 0x80070021):
                    raise failure
                continue
            try:
                if package.exists():
                    shutil.rmtree(package)
                failure = None
                break
            except OSError as exc:
                if getattr(exc, "winerror", None) not in (32, 33):
                    raise
                failure = exc
        if failure:
            raise failure
        if self.sid:
            bind(C.WinDLL("advapi32"), "FreeSid", C.c_void_p, [C.c_void_p])(self.sid)
            self.sid = None
