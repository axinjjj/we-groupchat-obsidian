"""Open local paths and URLs with the current desktop shell."""
from __future__ import annotations

import os
import subprocess
import sys


def open_target(target: str, *, reveal: bool = False):
    """Open a path/URL, or reveal a local path in its file manager."""
    value = os.fspath(target)
    if sys.platform == "win32":
        if reveal:
            return subprocess.run(
                ["explorer.exe", f"/select,{os.path.normpath(value)}"],
                check=False,
            )
        os.startfile(value)
        return None
    command = ["open"]
    if reveal:
        command.append("-R")
    command.append(value)
    return subprocess.run(command, check=False)
