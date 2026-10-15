"""Launch suspended, assign an owned Job, then resume under AppContainer."""

import base64
import ctypes as C
import os
import subprocess
import time
import uuid
from ctypes import wintypes as W

from core.harness.windows_job import WindowsJob, bind

P = C.c_void_p


class SecurityAttributes(C.Structure):
    _fields_ = [("length", W.DWORD), ("descriptor", P), ("inherit", W.BOOL)]


class StartupInfo(C.Structure):
    _fields_ = [("cb", W.DWORD), ("reserved", W.LPWSTR), ("desktop", W.LPWSTR),
                ("title", W.LPWSTR), *[(n, W.DWORD) for n in
                 ("x", "y", "xs", "ys", "xc", "yc", "fill", "flags")],
                ("show", W.WORD), ("reserved_size", W.WORD), ("reserved_bytes", P),
                ("stdin", W.HANDLE), ("stdout", W.HANDLE), ("stderr", W.HANDLE)]


class StartupEx(C.Structure):
    _fields_ = [("info", StartupInfo), ("attrs", P)]


class ProcessInfo(C.Structure):
    _fields_ = [("process", W.HANDLE), ("thread", W.HANDLE),
                ("pid", W.DWORD), ("tid", W.DWORD)]


class Capabilities(C.Structure):
    _fields_ = [("sid", P), ("caps", P), ("count", W.DWORD), ("reserved", W.DWORD)]


def environment(root):
    return {"SystemRoot": os.environ["SystemRoot"], "WINDIR": os.environ["SystemRoot"],
            "PATHEXT": ".COM;.EXE",
            "TEMP": str(root / "Temp"), "TMP": str(root / "Temp"),
            "LOCALAPPDATA": str(root), "USERPROFILE": str(root),
            "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "POWERSHELL_TELEMETRY_OPTOUT": "1"}


class NativeProcess:
    def __init__(self, profile, script, *, isolated=True, read_only=True, cancel=None, capture=False):
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        self.job = WindowsJob(getattr(profile, "memory_bytes", None))
        self.process = ProcessInfo()
        self.handles = []
        self.attributes = None
        self.profile = profile
        self.output = profile.root / "output.txt"
        self.handshake = uuid.uuid4().hex
        self.cancel = cancel
        self.issued = False
        self.capture, self.pipes = capture, None
        try:
            startup = self._startup(isolated)
            self._launch(self._gated_script(script) if isolated and read_only else script, startup)
            if self.pipes:
                self.pipes.release_writers()
            self.job.assign(self.process.process)
            resume = bind(self.kernel, "ResumeThread", W.DWORD, [W.HANDLE])
            if resume(self.process.thread) == 0xFFFFFFFF:
                raise C.WinError(C.get_last_error())
            if isolated and read_only:
                self._seal_then_release()
        except BaseException as exc:
            exc.execution_issued = self.issued
            if self.process.process:
                bind(self.kernel, "TerminateProcess", W.BOOL,
                     [W.HANDLE, W.UINT])(self.process.process, 125)
            self.close()
            raise

    def _gated_script(self, script):
        ready = str(self.profile.work / (self.handshake + ".ready")).replace("'", "''")
        release = str(self.profile.work / (self.handshake + ".release")).replace("'", "''")
        payload = base64.b64encode(script.encode("utf-8")).decode("ascii")
        return (f"[IO.File]::WriteAllText('{ready}','ready');"
                f"while(-not [IO.File]::Exists('{release}')){{Start-Sleep -Milliseconds 20}};"
                "& ([scriptblock]::Create([Text.Encoding]::UTF8.GetString("
                f"[Convert]::FromBase64String('{payload}'))))")

    def _seal_then_release(self):
        from core.harness.windows_readonly import require_appcontainer, set_profile_access

        require_appcontainer(self.process.process)
        deadline = time.monotonic() + 15
        ready = self.profile.work / (self.handshake + ".ready")
        while not ready.is_file():
            self._check_cancel()
            if time.monotonic() >= deadline:
                raise OSError("trusted bootstrap did not become ready; command withheld")
            time.sleep(0.02)
        set_profile_access(self.profile)
        self.profile.sealed = True
        self._check_cancel()
        pending = self.profile.work / (self.handshake + ".pending")
        pending.write_text("release", encoding="utf-8")
        self.issued = True  # uncertain rename failure must never be reported as safe to retry
        pending.replace(self.profile.work / (self.handshake + ".release"))

    def _check_cancel(self):
        if self.cancel is not None and self.cancel.is_set():
            raise InterruptedError("command cancelled before release")

    def _startup(self, isolated):
        sa = SecurityAttributes(C.sizeof(SecurityAttributes), None, True)
        createfile = bind(self.kernel, "CreateFileW", W.HANDLE,
                          [W.LPCWSTR, W.DWORD, W.DWORD, C.POINTER(SecurityAttributes),
                           W.DWORD, W.DWORD, W.HANDLE])
        files = [("NUL", 0x80000000, 3)]
        if self.capture:
            from core.harness.windows_pipes import OutputPipes
            self.pipes = OutputPipes(self.kernel, sa)
        else:
            files.insert(0, (str(self.output), 0x40000000, 2))
        for path, access, disposition in files:
            handle = createfile(path, access, 3, C.byref(sa), disposition, 0x80, None)
            if handle == C.c_void_p(-1).value:
                raise C.WinError(C.get_last_error())
            self.handles.append(handle)
        initialize = bind(self.kernel, "InitializeProcThreadAttributeList", W.BOOL,
                          [P, W.DWORD, W.DWORD, C.POINTER(C.c_size_t)])
        size = C.c_size_t()
        count = 2 if isolated else 1
        initialize(None, count, 0, C.byref(size))
        self.attributes = C.create_string_buffer(size.value)
        if not initialize(self.attributes, count, 0, C.byref(size)):
            raise C.WinError(C.get_last_error())
        self.caps = Capabilities(self.profile.sid, None, 0, 0)
        inherited = [*self.handles, *(self.pipes.writers if self.pipes else [])]
        self.inherited = (W.HANDLE * len(inherited))(*inherited)
        if isolated:
            self._attribute(0x20009, C.byref(self.caps), C.sizeof(self.caps))
        self._attribute(0x20002, self.inherited, C.sizeof(self.inherited))
        startup = StartupEx()
        startup.info.cb = C.sizeof(StartupEx)
        startup.info.flags = 0x100
        startup.info.stdin = self.handles[-1]
        startup.info.stdout = self.pipes.writers[0] if self.pipes else self.handles[0]
        startup.info.stderr = self.pipes.writers[1] if self.pipes else self.handles[0]
        startup.attrs = C.cast(self.attributes, P)
        return startup

    def _attribute(self, number, value, size):
        update = bind(self.kernel, "UpdateProcThreadAttribute", W.BOOL,
                      [P, W.DWORD, C.c_size_t, P, C.c_size_t, P, P])
        if not update(self.attributes, 0, number, value, size, None, None):
            raise C.WinError(C.get_last_error())

    def _launch(self, script, startup):
        bootstrap = ("[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);"
                     "$OutputEncoding=[Console]::OutputEncoding;"
                     "if($PSVersionTable.PSVersion.Major -lt 7){exit 126};"
                     "New-PSDrive -Name Workspace -PSProvider FileSystem "
                     "-Root ([Environment]::CurrentDirectory) -ErrorAction Stop | Out-Null;"
                     "Set-Location Workspace:\\ -ErrorAction Stop;" + script)
        encoded = base64.b64encode(bootstrap.encode("utf-16-le")).decode("ascii")
        executable = str(self.profile.runtime / "pwsh.exe")
        command = C.create_unicode_buffer(subprocess.list2cmdline(
            [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-OutputFormat", "Text",
             "-EncodedCommand", encoded]))
        env = C.create_unicode_buffer("\0".join(
            key + "=" + value for key, value in sorted(environment(self.profile.root).items())) + "\0\0")
        create = bind(self.kernel, "CreateProcessW", W.BOOL,
                      [W.LPCWSTR, W.LPWSTR, P, P, W.BOOL, W.DWORD, P, W.LPCWSTR,
                       C.POINTER(StartupEx), C.POINTER(ProcessInfo)])
        if not create(executable, command, None, None, True, 0x08080404, env,
                      str(self.profile.work), C.byref(startup), C.byref(self.process)):
            raise C.WinError(C.get_last_error())

    def wait(self, timeout, cancel):
        wait = bind(self.kernel, "WaitForSingleObject", W.DWORD, [W.HANDLE, W.DWORD])
        deadline = time.monotonic() + timeout
        status = "exited"
        while True:
            state = wait(self.process.process, 25)
            if state == 0:
                break
            if state != 258:
                raise OSError("owned process wait failed")
            if cancel.is_set() or time.monotonic() >= deadline:
                status = "cancelled" if cancel.is_set() else "timeout"
                self.job.terminate()
                if wait(self.process.process, 5000) != 0:
                    raise OSError("owned process termination unconfirmed")
                break
        code = W.DWORD()
        if not bind(self.kernel, "GetExitCodeProcess", W.BOOL,
                    [W.HANDLE, C.POINTER(W.DWORD)])(self.process.process, C.byref(code)):
            raise C.WinError(C.get_last_error())
        # Even a normally exiting parent may leave descendants: stop the entire owned tree.
        self.job.terminate()
        return status, code.value

    def close(self):
        failure = None
        try:
            if self.job.handle:
                self.job.terminate()
        except OSError as exc:
            failure = exc
        finally:
            self.job.close()
        close = bind(self.kernel, "CloseHandle", W.BOOL, [W.HANDLE])
        for handle in (self.process.thread, self.process.process, *self.handles):
            if handle:
                close(handle)
        self.handles.clear()
        if self.pipes:
            self.pipes.close()
        self.process = ProcessInfo()
        if self.attributes is not None:
            bind(self.kernel, "DeleteProcThreadAttributeList", None, [P])(self.attributes)
            self.attributes = None
        if failure:
            raise failure

    def poll(self):
        state = bind(self.kernel, "WaitForSingleObject", W.DWORD,
                     [W.HANDLE, W.DWORD])(self.process.process, 0)
        if state == 258:
            return None
        if state != 0:
            raise OSError("owned process wait failed")
        code = W.DWORD()
        if not bind(self.kernel, "GetExitCodeProcess", W.BOOL,
                    [W.HANDLE, C.POINTER(W.DWORD)])(self.process.process, C.byref(code)):
            raise C.WinError(C.get_last_error())
        return code.value

    def creation_time(self):
        times = [W.FILETIME() for _ in range(4)]
        if not bind(self.kernel, "GetProcessTimes", W.BOOL,
                    [W.HANDLE, *([C.POINTER(W.FILETIME)] * 4)])(
                        self.process.process, *(C.byref(t) for t in times)):
            raise C.WinError(C.get_last_error())
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
