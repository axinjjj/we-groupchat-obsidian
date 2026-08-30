"""Long-lived menu-app process ownership."""
from __future__ import annotations

import os

from .config import DATA_DIR, ensure_private_dir
from .platform import LockBusy, LockMode, create_file_lock


class AppAlreadyRunning(RuntimeError):
    """The canonical menu-app singleton lock is owned by another process."""


class AppInstanceLock:
    """Retain an advisory lock for the lifetime of the menu-bar process."""

    def __init__(self, path=None, *, file_lock=None):
        self.path = os.path.abspath(os.path.expanduser(
            path or os.path.join(DATA_DIR, "menu-app.lock")
        ))
        self._file_lock = file_lock
        self._lock_handle = None

    def _lock_service(self):
        if self._file_lock is None:
            self._file_lock = create_file_lock()
        return self._file_lock

    def acquire(self):
        if self._lock_handle is not None:
            return self
        ensure_private_dir(os.path.dirname(self.path))
        try:
            try:
                lock_handle = self._lock_service().acquire(
                    self.path,
                    mode=LockMode.EXCLUSIVE,
                    blocking=False,
                )
            except LockBusy as exc:
                raise AppAlreadyRunning("menu_app_already_running") from exc
            fd = lock_handle.fileno()
            os.ftruncate(fd, 0)
            os.write(fd, (str(os.getpid()) + "\n").encode("ascii"))
            try:
                os.fsync(fd)
            except OSError:
                pass
            self._lock_handle = lock_handle
            return self
        except Exception:
            if "lock_handle" in locals():
                lock_handle.close()
            raise

    def release(self):
        if self._lock_handle is None:
            return
        lock_handle = self._lock_handle
        self._lock_handle = None
        lock_handle.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, _exc_type, _exc, _traceback):
        self.release()
