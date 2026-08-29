"""Copy Unicode text through the current desktop clipboard."""
from __future__ import annotations

import subprocess
import sys
import time


def _copy_windows(text: str, *, attempts: int = 5) -> None:
    import win32clipboard

    last_error = None
    for attempt in range(attempts):
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
            finally:
                win32clipboard.CloseClipboard()
            return
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.05 * (attempt + 1))
    raise RuntimeError("Windows clipboard is busy") from last_error


def copy_text(text: str) -> None:
    """Copy text without passing it through a shell or command-line argument."""
    value = str(text)
    if sys.platform == "win32":
        _copy_windows(value)
        return
    subprocess.run(
        ["pbcopy"],
        input=value.encode("utf-8"),
        check=True,
        timeout=5,
    )
