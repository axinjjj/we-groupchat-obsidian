"""Run short-lived project jobs through the stable macOS app identity when present."""
from __future__ import annotations

import os
from pathlib import Path


RESOURCE_BACKUP_MODE = "--resource-backup-run"
SOURCE_GUARD_MODE = "--source-guard-run"


def app_bundle_executable(project_dir: str | os.PathLike[str]) -> Path | None:
    executable = (
        Path(project_dir).resolve()
        / "dist"
        / "WeGroupchatObsidian.app"
        / "Contents"
        / "MacOS"
        / "WeGroupchatObsidian"
    )
    if executable.is_file() and os.access(executable, os.X_OK):
        return executable.resolve()
    return None


def background_program_arguments(
    project_dir: str | os.PathLike[str],
    mode: str,
    python_fallback: list[str],
) -> tuple[list[str], str]:
    executable = app_bundle_executable(project_dir)
    if executable is not None:
        return [str(executable), mode], "app_bundle"
    return list(python_fallback), "python"


def runtime_identity(program_arguments: list[str] | tuple[str, ...]) -> str:
    args = [str(value) for value in program_arguments]
    if args and ".app/Contents/MacOS/" in args[0]:
        return "app_bundle"
    if args and (args[0].endswith("/python") or args[0].endswith("/python3")):
        return "python"
    return "unknown"


def dispatch_background_job(argv: list[str] | tuple[str, ...]) -> int | None:
    args = list(argv)
    if args == [RESOURCE_BACKUP_MODE]:
        from scripts.resource_backup import main

        return int(main(["run"]))
    if args == [SOURCE_GUARD_MODE]:
        from scripts.wechat_source_guard_agent import main

        return int(main())
    return None
