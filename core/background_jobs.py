"""Identify and reject retired short-lived protected-data job modes."""
from __future__ import annotations

import json


RESOURCE_BACKUP_MODE = "--resource-backup-run"
SOURCE_GUARD_MODE = "--source-guard-run"


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
        print(json.dumps({
            "state": "long_lived_app_required",
            "reason": "app_data_permission_is_process_lifetime",
        }, ensure_ascii=False))
        return 2
    if args == [SOURCE_GUARD_MODE]:
        print(json.dumps({
            "state": "long_lived_app_required",
            "reason": "app_data_permission_is_process_lifetime",
        }, ensure_ascii=False))
        return 2
    return None
