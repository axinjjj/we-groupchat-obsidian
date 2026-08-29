"""Long-lived menu-app process ownership."""
from __future__ import annotations

import os

from . import file_lock as fcntl
from .config import DATA_DIR, ensure_private_dir


class AppAlreadyRunning(RuntimeError):
    """The canonical menu-app singleton lock is owned by another process."""


class AppInstanceLock:
    """Retain an advisory lock for the lifetime of the menu-bar process."""

    def __init__(self, path=None):
        self.path = os.path.abspath(os.path.expanduser(
            path or os.path.join(DATA_DIR, "menu-app.lock")
        ))
        self._fd = None

    def acquire(self):
        if self._fd is not None:
            return self
        ensure_private_dir(os.path.dirname(self.path))
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise AppAlreadyRunning("menu_app_already_running") from exc
            os.ftruncate(fd, 0)
            os.write(fd, (str(os.getpid()) + "\n").encode("ascii"))
            try:
                os.fsync(fd)
            except OSError:
                pass
            self._fd = fd
            return self
        except Exception:
            os.close(fd)
            raise

    def release(self):
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self):
        return self.acquire()

    def __exit__(self, _exc_type, _exc, _traceback):
        self.release()
