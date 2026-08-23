#!/usr/bin/env python3
"""Configure and inspect the optional WeChat source guard."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import plistlib
import subprocess
import sys

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from core.config import load_config, save_config
from core.background_jobs import runtime_identity
from core.launch_agent import launch_agent_status
from core.project_identity import SOURCE_GUARD_LAUNCH_AGENT_LABEL
from core.wechat_source_guard import (
    WeChatSourceGuard,
    default_process_probe,
    pause_until_text,
    source_guard_status,
)


def agent_plist_path(home: str | os.PathLike[str] | None = None) -> Path:
    root = Path(home).expanduser() if home else Path.home()
    return root / "Library" / "LaunchAgents" / f"{SOURCE_GUARD_LAUNCH_AGENT_LABEL}.plist"


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True)


def install_agent(config: dict, *, load_now: bool = False, path: Path | None = None, runner=_run) -> int:
    del config, load_now, runner
    path = path or agent_plist_path()
    print(
        "Source guard LaunchAgent installation is retired: macOS App Data "
        "consent is tied to the running process. Enable the policy and keep "
        "the menu-bar app running instead."
    )
    if path.exists():
        print(f"Legacy plist still exists; remove it with uninstall-agent: {path}")
    return 2


def uninstall_agent(*, path: Path | None = None, runner=_run) -> int:
    path = path or agent_plist_path()
    domain = f"gui/{os.getuid()}"
    runner(["launchctl", "bootout", domain, str(path)])
    try:
        path.unlink()
        print(f"Removed: {path}")
    except FileNotFoundError:
        print("Source guard LaunchAgent plist is not installed.")
    return 0


def _set_config(key: str, value) -> dict:
    config = load_config()
    config[key] = value
    save_config(config)
    return load_config()


def print_status(config: dict) -> int:
    report = source_guard_status(config)
    process = default_process_probe()
    process_state = "unknown" if process is None else "running" if process else "absent"
    plist = agent_plist_path()
    agent = launch_agent_status(SOURCE_GUARD_LAUNCH_AGENT_LABEL)
    agent_identity = "not_installed"
    try:
        with plist.open("rb") as handle:
            payload = plistlib.load(handle)
        agent_identity = runtime_identity(payload.get("ProgramArguments") or [])
    except (OSError, plistlib.InvalidFileException, TypeError, ValueError):
        if plist.is_file():
            agent_identity = "unknown"
    print("WeChat source guard")
    print(f"  enabled: {report['enabled']}")
    print(f"  state: {report['state']}")
    print(f"  last result: {report['last_result']}")
    print(f"  WeChat process: {process_state}")
    print(f"  paused until: {report.get('pause_until') or '-'}")
    print(f"  missing duration: {int(report.get('missing_duration') or 0)}s")
    print(f"  restart budget remaining: {report['restart_budget_remaining']}")
    print(f"  backoff until: {report.get('backoff_until') or '-'}")
    print(f"  source freshness: {report.get('source_freshness') or 'unknown'}")
    print("  runtime: long_lived_app")
    print(f"  agent installed: {plist.exists()}")
    print(f"  agent loaded: {agent.loaded}")
    print(f"  agent runtime identity: {agent_identity}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="source-guard", description="Manage the optional WeChat source guard.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("enable")
    sub.add_parser("disable")
    pause = sub.add_parser("pause")
    group = pause.add_mutually_exclusive_group(required=True)
    group.add_argument("--hours", type=float)
    group.add_argument("--indefinite", action="store_true")
    sub.add_parser("resume")
    sub.add_parser("check")
    install = sub.add_parser("install-agent")
    install.add_argument("--load-now", action="store_true")
    sub.add_parser("uninstall-agent")
    args = parser.parse_args(argv)

    if args.command == "status":
        return print_status(load_config())
    if args.command == "enable":
        _set_config("wechat_source_guard_enabled", True)
        print("Source guard enabled for the long-lived menu-bar app.")
        return 0
    if args.command == "disable":
        _set_config("wechat_source_guard_enabled", False)
        print("Source guard disabled. Remove any legacy LaunchAgent separately.")
        return 0
    if args.command == "pause":
        if args.indefinite:
            value = "indefinite"
        else:
            if args.hours <= 0 or args.hours > 24 * 365:
                raise SystemExit("--hours must be greater than 0 and no more than 8760")
            value = pause_until_text(datetime.now(tz=timezone.utc).timestamp() + args.hours * 3600)
        _set_config("wechat_source_guard_pause_until", value)
        print(f"Source guard paused until: {value}")
        return 0
    if args.command == "resume":
        _set_config("wechat_source_guard_pause_until", "")
        print("Source guard pause cleared. Enabled and installation states are unchanged.")
        return 0
    if args.command == "check":
        result = WeChatSourceGuard(load_config()).check()
        print(f"state: {result['state']}")
        print(f"result: {result['last_result']}")
        return 2 if result["state"] == "degraded" else 0
    if args.command == "install-agent":
        return install_agent(load_config(), load_now=args.load_now)
    if args.command == "uninstall-agent":
        return uninstall_agent()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
