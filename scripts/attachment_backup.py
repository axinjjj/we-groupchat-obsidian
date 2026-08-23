#!/usr/bin/env python3
"""Plan, run, and verify provider-neutral attachment filesystem snapshots."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from core.attachment_backup import AttachmentBackup
from core.config import load_config, normalize_path_value, update_config


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="attachment-backup",
        description=(
            "Copy the local attachment CAS to a filesystem target. "
            "Verification covers target bytes, not any provider's cloud-upload state."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("plan")
    sub.add_parser("run")
    set_target = sub.add_parser("set-target")
    set_target.add_argument("path")
    sub.add_parser("clear-target")
    verify = sub.add_parser("verify")
    verify.add_argument("--snapshot-id", default="")
    restore = sub.add_parser("restore-plan")
    restore.add_argument("--snapshot-id", default="")
    args = parser.parse_args(argv)

    config = load_config()
    if args.command == "set-target":
        target = normalize_path_value(args.path)
        if not target:
            parser.error("path must not be empty")
        update_config(patch={"attachment_backup_target": target})
        _print({"state": "configured", "target": "configured"})
        return 0
    if args.command == "clear-target":
        update_config(patch={"attachment_backup_target": ""})
        _print({"state": "target_not_configured"})
        return 0

    backup = AttachmentBackup.from_config(config)
    if args.command == "status":
        result = backup.status()
    elif args.command == "plan":
        result = backup.plan()
    elif args.command == "run":
        result = backup.run()
    elif args.command == "verify":
        result = backup.verify(args.snapshot_id)
    elif args.command == "restore-plan":
        result = backup.restore_plan(args.snapshot_id)
    else:
        return 1
    _print(result)
    return 0 if result.get("state") not in {
        "failed",
        "target_failed",
        "catalog_unavailable",
        "invalid_target",
        "snapshot_unavailable",
    } else 2


if __name__ == "__main__":
    raise SystemExit(main())
