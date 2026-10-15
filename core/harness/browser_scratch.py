"""Only trusted browser payloads get a private cache; host and runtime remain sealed."""
import ctypes as C
from ctypes import wintypes as W

from core.harness.windows_job import bind


def grant(profile):
    from core.harness.windows_readonly import current_user_sid, reject_reparse_points, sid_string

    scratch = profile.root / 'Temp' / 'browser'
    if scratch.resolve() != scratch or not scratch.is_dir():
        raise PermissionError('browser scratch ownership mismatch')
    paths = reject_reparse_points(scratch)
    text = (f'D:P(A;OICI;FA;;;{current_user_sid()})(A;OICI;FA;;;SY)'
            f'(A;OICI;FA;;;{sid_string(profile.sid)})')
    advapi, descriptor = C.WinDLL('advapi32'), C.c_void_p()
    convert = bind(advapi, 'ConvertStringSecurityDescriptorToSecurityDescriptorW', W.BOOL,
                   [W.LPCWSTR, W.DWORD, C.POINTER(C.c_void_p), C.c_void_p])
    if not convert(text, 1, C.byref(descriptor), None):
        raise C.WinError(C.get_last_error())
    try:
        present, default, acl = W.BOOL(), W.BOOL(), C.c_void_p()
        get = bind(advapi, 'GetSecurityDescriptorDacl', W.BOOL,
                   [C.c_void_p, C.POINTER(W.BOOL), C.POINTER(C.c_void_p), C.POINTER(W.BOOL)])
        if not get(descriptor, C.byref(present), C.byref(acl), C.byref(default)):
            raise C.WinError(C.get_last_error())
        setter = bind(advapi, 'SetNamedSecurityInfoW', W.DWORD,
                      [W.LPCWSTR, C.c_int, W.DWORD, C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p])
        for path in paths:
            error = setter(str(path), 1, 0x80000004, None, None, acl, None)
            if error:
                raise C.WinError(error)
    finally:
        bind(C.WinDLL('kernel32'), 'LocalFree', C.c_void_p, [C.c_void_p])(descriptor)
