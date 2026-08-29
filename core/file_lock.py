"""Cross-platform advisory file locking with ``fcntl.flock`` semantics."""
from __future__ import annotations

import errno
import os


LOCK_SH = 1
LOCK_EX = 2
LOCK_NB = 4
LOCK_UN = 8


if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
    _LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
    _ERROR_LOCK_VIOLATION = 33
    _ERROR_NOT_LOCKED = 158
    _ERROR_IO_PENDING = 997

    class _Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _lock_file_ex = _kernel32.LockFileEx
    _lock_file_ex.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    _lock_file_ex.restype = wintypes.BOOL
    _unlock_file_ex = _kernel32.UnlockFileEx
    _unlock_file_ex.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    _unlock_file_ex.restype = wintypes.BOOL

    def _windows_handle(fd: int):
        raw_handle = msvcrt.get_osfhandle(fd)
        if raw_handle == -1:
            raise OSError(errno.EBADF, os.strerror(errno.EBADF))
        return wintypes.HANDLE(raw_handle)

    def flock(fd: int, operation: int) -> None:
        """Lock one sentinel byte, matching the supported ``flock`` operations."""
        supported_mask = LOCK_SH | LOCK_EX | LOCK_NB | LOCK_UN
        if operation & ~supported_mask:
            raise ValueError("unsupported file lock operation")

        handle = _windows_handle(fd)
        overlapped = _Overlapped()
        if operation & LOCK_UN:
            if operation != LOCK_UN:
                raise ValueError("LOCK_UN cannot be combined with another operation")
            if not _unlock_file_ex(handle, 0, 1, 0, ctypes.byref(overlapped)):
                error_code = ctypes.get_last_error()
                if error_code != _ERROR_NOT_LOCKED:
                    raise ctypes.WinError(error_code)
            return

        lock_kind = operation & (LOCK_SH | LOCK_EX)
        if lock_kind not in (LOCK_SH, LOCK_EX):
            raise ValueError("exactly one of LOCK_SH or LOCK_EX is required")
        flags = _LOCKFILE_EXCLUSIVE_LOCK if lock_kind == LOCK_EX else 0
        if operation & LOCK_NB:
            flags |= _LOCKFILE_FAIL_IMMEDIATELY
        if _lock_file_ex(handle, flags, 0, 1, 0, ctypes.byref(overlapped)):
            return

        error_code = ctypes.get_last_error()
        if operation & LOCK_NB and error_code in (
            _ERROR_LOCK_VIOLATION,
            _ERROR_IO_PENDING,
        ):
            raise BlockingIOError(errno.EACCES, "file lock is already held")
        raise ctypes.WinError(error_code)

else:
    import fcntl as _fcntl

    LOCK_SH = _fcntl.LOCK_SH
    LOCK_EX = _fcntl.LOCK_EX
    LOCK_NB = _fcntl.LOCK_NB
    LOCK_UN = _fcntl.LOCK_UN

    def flock(fd: int, operation: int) -> None:
        """Delegate to the native POSIX ``flock`` implementation."""
        _fcntl.flock(fd, operation)
