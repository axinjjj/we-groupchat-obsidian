"""Install a short-lived LaunchAgent for mounted selected-chat resource backup."""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import DATA_DIR


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


def _atomic_plist(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".partial-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            plistlib.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = ""
        os.chmod(path, 0o600)
    finally:
        if fd >= 0:
            os.close(fd)
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def install(project_dir: str | os.PathLike[str], interval_seconds: int = 300) -> dict:
    project = Path(project_dir).resolve()
    script = project / "scripts" / "resource_backup.py"
    if not script.is_file():
        return {"state": "script_missing", "installed": False, "loaded": False}
    interval = max(60, int(interval_seconds))
    logs = Path(DATA_DIR) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(logs, 0o700)
    except OSError:
        pass
    path = plist_path()
    environment = {"PYTHONUNBUFFERED": "1"}
    if os.environ.get("WE_GROUPCHAT_OBSIDIAN_DATA_DIR"):
        environment["WE_GROUPCHAT_OBSIDIAN_DATA_DIR"] = os.environ[
            "WE_GROUPCHAT_OBSIDIAN_DATA_DIR"
        ]
    payload = {
        "Label": RESOURCE_BACKUP_LAUNCH_AGENT_LABEL,
        "ProgramArguments": [sys.executable, str(script), "run"],
        "WorkingDirectory": str(project),
        "RunAtLoad": True,
        "StartInterval": interval,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "ThrottleInterval": 30,
        "StandardOutPath": str(logs / "resource-backup.out.log"),
        "StandardErrorPath": str(logs / "resource-backup.err.log"),
        "EnvironmentVariables": environment,
    }
    _run("launchctl", "bootout", _domain(), str(path))
    _atomic_plist(path, payload)
    loaded = _run("launchctl", "bootstrap", _domain(), str(path))
    if loaded.returncode != 0:
        return {
            "state": "install_failed",
            "installed": path.is_file(),
            "loaded": False,
            "error": (loaded.stderr or loaded.stdout).strip()[:240],
        }
    return {
        "state": "installed",
        "installed": True,
        "loaded": True,
        "interval_seconds": interval,
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
    return {
        "state": "loaded" if report.returncode == 0 else "not_loaded",
        "installed": path.is_file(),
        "loaded": report.returncode == 0,
    }
