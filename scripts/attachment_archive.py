#!/usr/bin/env python3
"""Inspect and explicitly operate the local attachment archive outbox."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from core.attachment_archive import AttachmentArchive
from core.config import load_config
from core.knowledge import KnowledgeStore


def _ensure_catalog(config):
    store = KnowledgeStore.from_config(config)
    conn = store.connect()
    conn.close()


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="attachment-archive",
        description="Manage the private, content-addressed WeChat attachment archive.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")

    run = sub.add_parser("run")
    run.add_argument("--limit", type=int, default=50)

    retry = sub.add_parser("retry")
    retry.add_argument("--mention-id", type=int, action="append", default=[])
    retry.add_argument("--run", action="store_true")
    retry.add_argument("--limit", type=int, default=50)

    backfill = sub.add_parser("backfill")
    backfill.add_argument(
        "--apply",
        action="store_true",
        help="Write the planned historical mentions. Without this flag, the command is read-only.",
    )
    backfill.add_argument("--run", action="store_true", help="Archive pending rows after --apply.")
    backfill.add_argument("--limit", type=int, default=50)
    args = parser.parse_args(argv)

    config = load_config()
    archive = AttachmentArchive.from_config(config)
    if args.command == "status":
        _print(archive.status())
        return 0

    _ensure_catalog(config)
    if args.command == "run":
        _print(archive.process_pending(limit=max(1, args.limit)))
        return 0
    if args.command == "retry":
        reset = archive.retry(mention_ids=args.mention_id)
        result = {"reset_to_pending": reset}
        if args.run:
            result["run"] = archive.process_pending(limit=max(1, args.limit))
        _print(result)
        return 0
    if args.command == "backfill":
        plan = archive.plan_backfill()
        result = {"mode": "apply" if args.apply else "plan", "plan": plan}
        if args.apply:
            result["inserted"] = archive.apply_backfill()
            if args.run:
                result["run"] = archive.process_pending(limit=max(1, args.limit))
        elif args.run:
            parser.error("--run requires --apply")
        _print(result)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
