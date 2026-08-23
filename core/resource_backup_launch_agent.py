"""Remove/inspect the retired short-lived mounted-resource LaunchAgent.

macOS App Data consent is process-lifetime access.  A worker that exits after
every interval therefore re-prompts on the next wake.  New installs run mounted
resource work inside the long-lived menu-bar app; this module remains only so
older installations can be detected and removed cleanly.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

from .background_jobs import runtime_identity


RESOURCE_BACKUP_LAUNCH_AGENT_LABEL = (
    "com.indeliblevivi.we-groupchat-obsidian.resource-backup"
)


def plist_path() -> Path:
    return (
        Path.home()
        / "Library"
        / "LaunchAgents"
        / f"{RESOURCE_BACKUP_LAUNCH_AGENT_LABEL}.plist"
    )


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _run(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(
            list(args),
            127,
            stdout="",
            stderr=type(exc).__name__,
        )


def install(project_dir: str | os.PathLike[str], interval_seconds: int = 300) -> dict:
    del project_dir, interval_seconds
    existing = status()
    return {
        "state": "long_lived_app_required",
        "installed": existing["installed"],
        "loaded": existing["loaded"],
        "runtime_identity": existing["runtime_identity"],
        "reason": "app_data_permission_is_process_lifetime",
    }


def uninstall() -> dict:
    path = plist_path()
    _run("launchctl", "bootout", _domain(), str(path))
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        return {
            "state": "uninstall_failed",
            "installed": path.exists(),
            "loaded": False,
            "error": type(exc).__name__,
        }
    return {"state": "uninstalled", "installed": False, "loaded": False}


def status() -> dict:
    path = plist_path()
    report = _run(
        "launchctl",
        "print",
        f"{_domain()}/{RESOURCE_BACKUP_LAUNCH_AGENT_LABEL}",
    )
    identity = "not_installed"
    try:
        with path.open("rb") as handle:
            payload = plistlib.load(handle)
        identity = runtime_identity(payload.get("ProgramArguments") or [])
    except (OSError, plistlib.InvalidFileException, TypeError, ValueError):
        if path.is_file():
            identity = "unknown"
    return {
        "state": "loaded" if report.returncode == 0 else "not_loaded",
        "installed": path.is_file(),
        "loaded": report.returncode == 0,
        "runtime_identity": identity,
    }
