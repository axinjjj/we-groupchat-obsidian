"""macOS advisory file locks backed by ``fcntl.flock``."""
from __future__ import annotations

import errno
import fcntl
import os
from os import PathLike

from .contracts import LockBusy, LockMode


class _MacOSLockHandle:
    def __init__(self, fd: int):
        self._fd = fd

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
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


class MacOSFileLock:
    """Retain shared or exclusive ``flock`` ownership until handle close."""

    def acquire(
        self,
        path: str | PathLike[str],
        *,
        mode: LockMode,
        blocking: bool,
    ) -> _MacOSLockHandle:
        if not isinstance(mode, LockMode):
            raise TypeError("mode must be a LockMode")
        fd = os.open(os.fspath(path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass
            operation = (
                fcntl.LOCK_SH if mode is LockMode.SHARED else fcntl.LOCK_EX
            )
            if not blocking:
                operation |= fcntl.LOCK_NB
            try:
                fcntl.flock(fd, operation)
            except OSError as exc:
                if not blocking and exc.errno in {errno.EACCES, errno.EAGAIN}:
                    raise LockBusy(LockBusy.code) from exc
                raise
            return _MacOSLockHandle(fd)
        except Exception:
            os.close(fd)
            raise
