"""LaunchAgent discovery and status helpers for the local macOS app."""
from __future__ import annotations

from dataclasses import dataclass
import os
import plistlib
import re
import subprocess
from pathlib import Path
from typing import Callable, Iterable

from .project_identity import LAUNCH_AGENT_LABEL, LEGACY_RUNTIME_DIR_NAMES, PROJECT_SLUG

DEFAULT_LABEL = LAUNCH_AGENT_LABEL
COMMAND_NAME = "启动.command"


@dataclass(frozen=True)
class LaunchAgentRecord:
    label: str
    plist_path: Path
    program_arguments: tuple[str, ...] = ()
    working_directory: str = ""
    match_kind: str = "exact"


@dataclass(frozen=True)
class LaunchAgentStatus:
    loaded: bool
    running: bool
    state: str = ""
    job_state: str = ""
    last_exit_code: str = ""


Runner = Callable[..., subprocess.CompletedProcess]


def launch_agents_dir(home: str | os.PathLike[str] | None = None) -> Path:
    root = Path(home).expanduser() if home else Path.home()
    return root / "Library" / "LaunchAgents"


def plist_path_for_label(
    label: str = DEFAULT_LABEL,
    home: str | os.PathLike[str] | None = None,
    launch_agents_dir: str | os.PathLike[str] | None = None,
) -> Path:
    base = Path(launch_agents_dir) if launch_agents_dir else globals()["launch_agents_dir"](home)
    return base / f"{label}.plist"


def default_launch_agent_record(
    project_dir: str | os.PathLike[str],
    home: str | os.PathLike[str] | None = None,
    launch_agents_dir: str | os.PathLike[str] | None = None,
) -> LaunchAgentRecord:
    return LaunchAgentRecord(
        DEFAULT_LABEL,
        plist_path_for_label(DEFAULT_LABEL, home=home, launch_agents_dir=launch_agents_dir),
        ("/bin/bash", str(Path(project_dir).resolve() / COMMAND_NAME), "--autostart"),
        str(Path(project_dir).resolve()),
    )


def _norm_path(value: str | os.PathLike[str] | None) -> str:
    if not value:
        return ""
    return os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(str(value)))))


def _path_is_within(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> bool:
    path_norm = _norm_path(path)
    root_norm = _norm_path(root)
    if not path_norm or not root_norm:
        return False
    try:
        return os.path.commonpath([path_norm, root_norm]) == root_norm
    except ValueError:
        return False


def _points_to_project(project_dir: Path, program_arguments: Iterable[str], working_directory: str) -> bool:
    project_path = _norm_path(project_dir)
    if working_directory and _norm_path(working_directory) == project_path:
        return True
    command_path = _norm_path(project_dir / COMMAND_NAME)
    for arg in program_arguments:
        if _norm_path(arg) == command_path:
            return True
    return False


def _points_to_same_named_runtime(project_dir: Path, program_arguments: Iterable[str], working_directory: str) -> bool:
    project_names = {project_dir.name, PROJECT_SLUG, *LEGACY_RUNTIME_DIR_NAMES}
    project_names = {name for name in project_names if name}
    if not project_names:
        return False
    command_seen = False
    for arg in program_arguments:
        path = Path(str(arg))
        if path.name == COMMAND_NAME:
            command_seen = True
            if path.parent.name in project_names:
                return True
    if working_directory:
        wd = Path(str(working_directory))
        if wd.name in project_names:
            for arg in program_arguments:
                if _path_is_within(arg, wd):
                    return True
        return command_seen and wd.name in project_names
    return False


def load_launch_agent_record(
    plist_path: str | os.PathLike[str],
    project_dir: str | os.PathLike[str],
    include_related: bool = False,
) -> LaunchAgentRecord | None:
    path = Path(plist_path)
    try:
        with path.open("rb") as handle:
            data = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    label = data.get("Label")
    program_arguments = data.get("ProgramArguments") or ()
    working_directory = data.get("WorkingDirectory") or ""
    if not isinstance(label, str) or not label.strip():
        return None
    if not isinstance(program_arguments, (list, tuple)):
        program_arguments = ()
    args = tuple(str(arg) for arg in program_arguments)
    resolved_project = Path(project_dir).resolve()
    if _points_to_project(resolved_project, args, str(working_directory)):
        match_kind = "exact"
    elif include_related and _points_to_same_named_runtime(resolved_project, args, str(working_directory)):
        match_kind = "runtime-copy"
    else:
        return None
    return LaunchAgentRecord(label.strip(), path, args, str(working_directory), match_kind)


def discover_managed_launch_agents(
    project_dir: str | os.PathLike[str],
    launch_agents_dir: str | os.PathLike[str] | None = None,
    include_related: bool = False,
) -> list[LaunchAgentRecord]:
    base = Path(launch_agents_dir) if launch_agents_dir else globals()["launch_agents_dir"]()
    if not base.is_dir():
        return []
    records = []
    for path in sorted(base.glob("*.plist")):
        record = load_launch_agent_record(path, project_dir, include_related=include_related)
        if record:
            records.append(record)
    return records


def choose_install_record(
    project_dir: str | os.PathLike[str],
    launch_agents_dir: str | os.PathLike[str] | None = None,
    migrate_label: bool = False,
) -> LaunchAgentRecord:
    default_record = default_launch_agent_record(project_dir, launch_agents_dir=launch_agents_dir)
    if migrate_label:
        return default_record
    records = discover_managed_launch_agents(project_dir, launch_agents_dir, include_related=True)
    if not records:
        return default_record
    for record in records:
        if record.label == DEFAULT_LABEL:
            return record
    return records[0]


def _match_launchctl_field(output: str, field: str) -> str:
    match = re.search(rf"^\s*{re.escape(field)}\s*=\s*(.+?)\s*$", output, re.MULTILINE)
    return match.group(1).strip() if match else ""


def parse_launch_agent_status(returncode: int, stdout: str) -> LaunchAgentStatus:
    loaded = returncode == 0
    state = _match_launchctl_field(stdout, "state")
    job_state = _match_launchctl_field(stdout, "job state")
    last_exit_code = _match_launchctl_field(stdout, "last exit code")
    running = loaded and (state == "running" or job_state == "running")
    return LaunchAgentStatus(
        loaded=loaded,
        running=running,
        state=state,
        job_state=job_state,
        last_exit_code=last_exit_code,
    )


def launch_agent_status(
    label: str,
    runner: Runner = subprocess.run,
    uid: int | None = None,
) -> LaunchAgentStatus:
    user_id = os.getuid() if uid is None else uid
    result = runner(
        ["launchctl", "print", f"gui/{user_id}/{label}"],
        capture_output=True,
        text=True,
    )
    return parse_launch_agent_status(result.returncode, result.stdout)


def launch_agent_report(
    project_dir: str | os.PathLike[str],
    launch_agents_dir: str | os.PathLike[str] | None = None,
    runner: Runner = subprocess.run,
    uid: int | None = None,
) -> tuple[LaunchAgentRecord, LaunchAgentStatus]:
    records = discover_managed_launch_agents(project_dir, launch_agents_dir, include_related=True)
    candidates = records or [default_launch_agent_record(project_dir, launch_agents_dir=launch_agents_dir)]
    checked = [(record, launch_agent_status(record.label, runner=runner, uid=uid)) for record in candidates]
    checked.sort(key=lambda item: (not item[1].running, not item[1].loaded, item[0].label != DEFAULT_LABEL))
    return checked[0]
