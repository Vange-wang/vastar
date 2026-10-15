"""Temporary unused DOS drive for an owned sealed package; never changes host ACLs."""
import ctypes
import threading

LOCK = threading.Lock()


def attach(profile):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    api = kernel.DefineDosDeviceW
    api.argtypes = [ctypes.c_ulong, ctypes.c_wchar_p, ctypes.c_wchar_p]
    api.restype = ctypes.c_int
    target = '\\??\\' + str(profile.work.resolve())
    query = kernel.QueryDosDeviceW
    query.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong]
    query.restype = ctypes.c_ulong
    with LOCK:
        for letter in reversed('GHIJKLMNOPQRSTUVWXYZ'):
            name, buffer = letter + ':', ctypes.create_unicode_buffer(32768)
            if query(name, buffer, len(buffer)) or ctypes.get_last_error() != 2:
                continue
            if not api(1 | 8, name, target):
                raise ctypes.WinError(ctypes.get_last_error())
            break
        else:
            raise OSError('No unused private validation drive available')
    original, released = profile.close, False

    def close():
        nonlocal released
        try:
            original()
        finally:
            if not released:
                if not api(1 | 2 | 4 | 8, name, target):
                    raise ctypes.WinError(ctypes.get_last_error())
                released = True
    profile.close = close
    return name + '\\'
