#!/usr/bin/env python3
"""Capture selected-chat links/files and hand them to a mounted backup target."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import time

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from core.config import (
    load_config,
    normalize_path_value,
    save_config,
    selected_resource_backup_chats,
)
from core.key_extractor import get_cached_keys
from core.resource_backup import (
    MountedResourceBackup,
    load_resource_backup_settings,
    save_resource_backup_settings,
)
from core.resource_backup_launch_agent import (
    install as install_resource_backup_agent,
    status as resource_backup_agent_status,
    uninstall as uninstall_resource_backup_agent,
)
from core.resource_capture import SelectedResourceCapture, resource_backup_chat_candidates
from core.wechat_db import WeChatDB


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _source(config):
    keys = get_cached_keys() or {}
    if not keys or not config.get("db_dir"):
        return None
    return WeChatDB(config["db_dir"], keys)


def _from_timestamp(value):
    try:
        return int(datetime.strptime(value, "%Y-%m-%d").timestamp())
    except ValueError as exc:
        raise SystemExit("--from must use YYYY-MM-DD") from exc


def _capture(config, *, source=False):
    return SelectedResourceCapture.from_config(
        config,
        source=_source(config) if source else None,
    )


def _backup(config, *, capture=None, link_export_mode=None):
    return MountedResourceBackup.from_config(
        config,
        capture=capture,
        link_export_mode=link_export_mode,
    )


def _exit_code(result):
    state = str((result or {}).get("state") or "")
    if state in {
        "invalid_target",
        "target_failed",
        "snapshot_unavailable",
        "source_degraded",
        "degraded",
        "source_unavailable",
        "destination_unavailable",
        "target_not_configured",
        "no_selected_chats",
        "pending_resources",
        "worker_busy",
        "install_failed",
        "uninstall_failed",
        "script_missing",
        "long_lived_app_required",
    }:
        return 2
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="resource-backup",
        description=(
            "Capture links/files only from explicitly selected active monitor chats, "
            "write Obsidian resource indexes, and hand CAS objects to a mounted "
            "filesystem target. Mounted handoff is not remote cloud verification."
        ),
    )
    parser.add_argument(
        "--link-export-mode",
        choices=("redacted", "full", "off"),
        default=None,
        help="How exact link URLs are written to the mounted backup catalog.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("list-chats")
    selected = sub.add_parser("set-selected-chats")
    selected.add_argument("indexes", nargs="+", type=int)
    sub.add_parser("clear-selected-chats")
    sub.add_parser("enable")
    sub.add_parser("disable")
    sub.add_parser("enable-file-resolution")
    sub.add_parser("disable-file-resolution")
    sub.add_parser("init")
    sub.add_parser("scan")
    backfill = sub.add_parser("backfill")
    backfill_scope = backfill.add_mutually_exclusive_group(required=True)
    backfill_scope.add_argument("--from", dest="from_date")
    backfill_scope.add_argument(
        "--all",
        action="store_true",
        help="Scan all locally available history for the selected chats.",
    )
    backfill.add_argument("--apply", action="store_true")
    backfill_links = sub.add_parser(
        "backfill-links",
        help="Plan/apply exact historical links without resolving attachment files.",
    )
    backfill_links_scope = backfill_links.add_mutually_exclusive_group(required=True)
    backfill_links_scope.add_argument("--from", dest="from_date")
    backfill_links_scope.add_argument(
        "--all",
        action="store_true",
        help="Scan all locally available history for exact links.",
    )
    backfill_links.add_argument("--apply", action="store_true")
    resolve = sub.add_parser("resolve")
    resolve.add_argument("--limit", type=int, default=50)
    sub.add_parser("index")
    sub.add_parser("plan")
    run = sub.add_parser("run")
    run.add_argument("--resolve-limit", type=int, default=50)
    run.add_argument(
        "--resolve-files",
        action="store_true",
        help="Explicitly allow this run to read selected WeChat attachment files.",
    )
    verify = sub.add_parser("verify")
    verify.add_argument("--snapshot-id", default="")
    target = sub.add_parser("set-target")
    target.add_argument("path")
    sub.add_parser("clear-target")
    mode = sub.add_parser("set-link-export-mode")
    mode.add_argument("mode", choices=("redacted", "full", "off"))
    install_agent = sub.add_parser("install-agent")
    install_agent.add_argument("--interval-seconds", type=int, default=None)
    sub.add_parser("uninstall-agent")
    sub.add_parser("agent-status")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    config = load_config()

    if args.command == "list-chats":
        selected_usernames = {
            chat["username"] for chat in selected_resource_backup_chats(config)
        }
        choices = resource_backup_chat_candidates(config)
        _print({
            "state": "ok",
            "chats": [
                {
                    "index": index,
                    "alias": chat["alias"],
                    "selected": chat["username"] in selected_usernames,
                }
                for index, chat in enumerate(choices, 1)
            ],
        })
        return 0
    if args.command == "set-selected-chats":
        choices = resource_backup_chat_candidates(config)
        indexes = list(dict.fromkeys(args.indexes))
        if not indexes or any(index < 1 or index > len(choices) for index in indexes):
            _print({"state": "invalid_selection", "available_chats": len(choices)})
            return 2
        previous = {
            chat["username"]: int(chat.get("selected_since") or 0)
            for chat in selected_resource_backup_chats(config)
        }
        now = int(time.time())
        selected = []
        for index in indexes:
            chat = dict(choices[index - 1])
            chat["selected_since"] = previous.get(chat["username"]) or now
            selected.append(chat)
        updated = dict(config)
        updated["resource_backup_selected_chats"] = selected
        save_config(updated)
        _print({
            "state": "configured",
            "selected_chats": len(selected),
            "aliases": [chat["alias"] for chat in selected],
        })
        return 0
    if args.command == "clear-selected-chats":
        updated = dict(config)
        updated["resource_backup_selected_chats"] = []
        save_config(updated)
        _print({"state": "configured", "selected_chats": 0})
        return 0
    if args.command in {
        "enable",
        "disable",
        "enable-file-resolution",
        "disable-file-resolution",
    }:
        updated = dict(config)
        if args.command in {"enable", "disable"}:
            updated["resource_backup_enabled"] = args.command == "enable"
        else:
            updated["resource_backup_file_resolution_enabled"] = (
                args.command == "enable-file-resolution"
            )
        save_config(updated)
        _print({
            "state": "configured",
            "background_enabled": bool(updated.get("resource_backup_enabled", False)),
            "file_resolution_enabled": bool(
                updated.get("resource_backup_file_resolution_enabled", False)
            ),
            "runtime": "long_lived_app",
        })
        return 0

    if args.command == "set-target":
        target = normalize_path_value(args.path)
        if not target:
            raise SystemExit("target path must not be empty")
        settings = save_resource_backup_settings({"target": target})
        _print({"state": "configured", "target": "configured", "link_export_mode": settings["link_export_mode"]})
        return 0
    if args.command == "clear-target":
        settings = save_resource_backup_settings({"target": ""})
        _print({"state": "target_not_configured", "link_export_mode": settings["link_export_mode"]})
        return 0
    if args.command == "set-link-export-mode":
        settings = save_resource_backup_settings({"link_export_mode": args.mode})
        _print({
            "state": "configured",
            "target": "configured" if settings["target"] else "not configured",
            "link_export_mode": settings["link_export_mode"],
        })
        return 0
    if args.command == "install-agent":
        interval = (
            args.interval_seconds
            if args.interval_seconds is not None
            else config.get("resource_backup_interval_seconds", 300)
        )
        result = install_resource_backup_agent(PROJECT_DIR, interval)
        _print(result)
        return _exit_code(result)
    if args.command == "uninstall-agent":
        result = uninstall_resource_backup_agent()
        _print(result)
        return _exit_code(result)
    if args.command == "agent-status":
        result = resource_backup_agent_status()
        _print(result)
        return 0

    if args.command == "init":
        result = _capture(config).initialize_selected_chat_cursors()
    elif args.command == "scan":
        result = _capture(config, source=True).scan()
    elif args.command == "backfill":
        result = _capture(config, source=True).backfill(
            0 if args.all else _from_timestamp(args.from_date),
            apply=args.apply,
        )
    elif args.command == "backfill-links":
        result = _capture(config, source=True).backfill_links(
            0 if args.all else _from_timestamp(args.from_date),
            apply=args.apply,
        )
    elif args.command == "resolve":
        result = _capture(config).resolve_pending_files(limit=max(1, args.limit))
    elif args.command == "index":
        result = _backup(
            config,
            link_export_mode=args.link_export_mode,
        ).render_obsidian_indexes()
    elif args.command == "plan":
        result = _backup(
            config,
            link_export_mode=args.link_export_mode,
        ).plan()
    elif args.command == "status":
        capture = _capture(config)
        result = {
            "state": "ok",
            "settings": {
                "target": "configured" if load_resource_backup_settings()["target"] else "not configured",
                "link_export_mode": load_resource_backup_settings()["link_export_mode"],
                "background_enabled": bool(config.get("resource_backup_enabled", False)),
                "file_resolution_enabled": bool(
                    config.get("resource_backup_file_resolution_enabled", False)
                ),
                "runtime": "long_lived_app",
            },
            "capture": capture.status(),
            "backup": _backup(
                config,
                capture=capture,
                link_export_mode=args.link_export_mode,
            ).status(),
            "launch_agent": resource_backup_agent_status(),
        }
    elif args.command == "run":
        capture = _capture(config, source=True)
        capture_result = capture.run(
            resolve_limit=max(1, args.resolve_limit),
            resolve_files=bool(args.resolve_files),
        )
        backup_result = _backup(
            config,
            capture=capture,
            link_export_mode=args.link_export_mode,
        ).run()
        state = "ok"
        capture_state = str(capture_result.get("state") or "")
        backup_state = str(backup_result.get("state") or "")
        if capture_state in {"degraded", "source_degraded", "source_unavailable"}:
            state = capture_state
        if backup_state in {
            "invalid_target", "target_failed", "target_not_configured",
            "destination_unavailable", "no_selected_chats", "worker_busy",
            "pending_resources",
        }:
            state = backup_state
        result = {
            "state": state,
            "capture": capture_result,
            "backup": backup_result,
        }
    elif args.command == "verify":
        result = _backup(
            config,
            link_export_mode=args.link_export_mode,
        ).verify(args.snapshot_id)
    else:
        return 1

    _print(result)
    return _exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
