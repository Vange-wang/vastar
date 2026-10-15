"""Seal only an owned AppContainer package tree; no host project ACL mutation."""

import ctypes as C
from ctypes import wintypes as W

from core.harness.windows_job import bind


def sid_string(sid):
    value = W.LPWSTR()
    convert = bind(C.WinDLL("advapi32"), "ConvertSidToStringSidW", W.BOOL,
                   [C.c_void_p, C.POINTER(W.LPWSTR)])
    if not convert(sid, C.byref(value)):
        raise C.WinError(C.get_last_error())
    try:
        return value.value
    finally:
        bind(C.WinDLL("kernel32"), "LocalFree", C.c_void_p, [C.c_void_p])(value)


def current_user_sid():
    kernel = C.WinDLL("kernel32", use_last_error=True)
    advapi = C.WinDLL("advapi32", use_last_error=True)
    process = bind(kernel, "GetCurrentProcess", W.HANDLE, [])()
    token = W.HANDLE()
    if not bind(advapi, "OpenProcessToken", W.BOOL,
                [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)])(process, 8, C.byref(token)):
        raise C.WinError(C.get_last_error())
    try:
        size = W.DWORD()
        get = bind(advapi, "GetTokenInformation", W.BOOL,
                   [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)])
        get(token, 1, None, 0, C.byref(size))
        buffer = C.create_string_buffer(size.value)
        if not get(token, 1, buffer, size.value, C.byref(size)):
            raise C.WinError(C.get_last_error())
        return sid_string(C.c_void_p.from_buffer(buffer).value)
    finally:
        bind(kernel, "CloseHandle", W.BOOL, [W.HANDLE])(token)


def set_profile_access(profile, *, read_only=True):
    root = profile.root.resolve().parent
    if root.name.casefold() != profile.name.casefold():
        raise PermissionError("profile ownership mismatch; refusing ACL mutation")
    paths = reject_reparse_points(root)
    # Replace inherited package FullControl, rather than adding deny ACEs. The
    # AppContainer package access check does not provide a useful deny-ACE boundary.
    rights = "GRGX" if read_only else "FA"
    sddl = (f"D:P(A;OICI;FA;;;{current_user_sid()})(A;OICI;FA;;;SY)"
            f"(A;OICI;{rights};;;{sid_string(profile.sid)})")
    advapi = C.WinDLL("advapi32", use_last_error=True)
    descriptor = C.c_void_p()
    convert = bind(advapi, "ConvertStringSecurityDescriptorToSecurityDescriptorW", W.BOOL,
                   [W.LPCWSTR, W.DWORD, C.POINTER(C.c_void_p), C.c_void_p])
    if not convert(sddl, 1, C.byref(descriptor), None):
        raise C.WinError(C.get_last_error())
    try:
        present, default = W.BOOL(), W.BOOL()
        acl = C.c_void_p()
        get = bind(advapi, "GetSecurityDescriptorDacl", W.BOOL,
                   [C.c_void_p, C.POINTER(W.BOOL), C.POINTER(C.c_void_p), C.POINTER(W.BOOL)])
        if not get(descriptor, C.byref(present), C.byref(acl), C.byref(default)):
            raise C.WinError(C.get_last_error())
        setter = bind(advapi, "SetNamedSecurityInfoW", W.DWORD,
                      [W.LPWSTR, C.c_int, W.DWORD, C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p])
        for path in paths:
            result = setter(str(path), 1, 0x80000004, None, None, acl, None)
            if result:
                raise C.WinError(result)
    finally:
        bind(C.WinDLL("kernel32"), "LocalFree", C.c_void_p, [C.c_void_p])(descriptor)
    if getattr(profile, 'browser_scratch', False):
        from core.harness.browser_scratch import grant
        grant(profile)


def reject_reparse_points(root):
    pending = [root]
    paths = []
    while pending:
        current = pending.pop()
        if current.lstat().st_file_attributes & 0x400:
            raise PermissionError("profile contains a reparse point; command withheld")
        paths.append(current)
        if current.is_dir():
            pending.extend(current.iterdir())
    return paths


def require_appcontainer(process):
    advapi = C.WinDLL("advapi32", use_last_error=True)
    token = W.HANDLE()
    if not bind(advapi, "OpenProcessToken", W.BOOL,
                [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)])(process, 8, C.byref(token)):
        raise C.WinError(C.get_last_error())
    try:
        value, size = W.DWORD(), W.DWORD()
        get = bind(advapi, "GetTokenInformation", W.BOOL,
                   [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)])
        if not get(token, 29, C.byref(value), 4, C.byref(size)) or value.value != 1:
            raise PermissionError("process token is not AppContainer; command withheld")
    finally:
        bind(C.WinDLL("kernel32"), "CloseHandle", W.BOOL, [W.HANDLE])(token)
