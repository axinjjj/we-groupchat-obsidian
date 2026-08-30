"""Windows advisory file locks backed by ``LockFileEx``."""
from __future__ import annotations

import ctypes
import errno
import msvcrt
import os
from ctypes import wintypes
from os import PathLike

from .contracts import LockBusy, LockMode


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


def _windows_handle(fd: int) -> wintypes.HANDLE:
    raw_handle = msvcrt.get_osfhandle(fd)
    if raw_handle == -1:
        raise OSError(errno.EBADF, os.strerror(errno.EBADF))
    return wintypes.HANDLE(raw_handle)


class _WindowsLockHandle:
    def __init__(self, fd: int, overlapped: _Overlapped):
        self._fd = fd
        self._overlapped = overlapped

    def fileno(self) -> int:
        if self._fd is None:
            raise ValueError("lock handle is closed")
        return self._fd

    def close(self) -> None:
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        try:
            if not _unlock_file_ex(
                _windows_handle(fd),
                0,
                1,
                0,
                ctypes.byref(self._overlapped),
            ):
                error_code = ctypes.get_last_error()
                if error_code != _ERROR_NOT_LOCKED:
                    raise ctypes.WinError(error_code)
        finally:
            os.close(fd)

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


class WindowsFileLock:
    """Retain one shared or exclusive sentinel-byte lock until handle close."""

    def acquire(
        self,
        path: str | PathLike[str],
        *,
        mode: LockMode,
        blocking: bool,
    ) -> _WindowsLockHandle:
        if not isinstance(mode, LockMode):
            raise TypeError("mode must be a LockMode")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
        fd = os.open(os.fspath(path), flags, 0o600)
        overlapped = _Overlapped()
        try:
            lock_flags = (
                _LOCKFILE_EXCLUSIVE_LOCK
                if mode is LockMode.EXCLUSIVE
                else 0
            )
            if not blocking:
                lock_flags |= _LOCKFILE_FAIL_IMMEDIATELY
            if not _lock_file_ex(
                _windows_handle(fd),
                lock_flags,
                0,
                1,
                0,
                ctypes.byref(overlapped),
            ):
                error_code = ctypes.get_last_error()
                if not blocking and error_code in {
                    _ERROR_LOCK_VIOLATION,
                    _ERROR_IO_PENDING,
                }:
                    raise LockBusy(LockBusy.code)
                raise ctypes.WinError(error_code)
            return _WindowsLockHandle(fd, overlapped)
        except Exception:
            os.close(fd)
            raise
