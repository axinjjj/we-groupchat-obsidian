"""Small Windows command-line encoding boundary for user-facing scripts."""
from __future__ import annotations

import sys


def configure_utf8_stdio() -> None:
    """Keep redirected Windows CLI output from failing on Chinese text."""
    if sys.platform != "win32":
        return
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue
