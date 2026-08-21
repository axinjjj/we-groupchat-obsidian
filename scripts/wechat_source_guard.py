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
import tempfile

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from core.config import DATA_DIR, load_config, save_config
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


def build_agent_plist(config: dict, project_dir: Path = PROJECT_DIR) -> dict:
    interval = int(config.get("wechat_source_guard_interval_seconds", 300) or 300)
    python = project_dir / ".venv" / "bin" / "python"
    entrypoint = project_dir / "scripts" / "wechat_source_guard_agent.py"
    payload = {
        "Label": SOURCE_GUARD_LAUNCH_AGENT_LABEL,
        "ProgramArguments": [str(python), str(entrypoint)],
        "WorkingDirectory": str(project_dir),
        "RunAtLoad": True,
        "StartInterval": interval,
        "ProcessType": "Background",
        "StandardOutPath": str(Path(DATA_DIR) / "logs" / "source-guard.out.log"),
        "StandardErrorPath": str(Path(DATA_DIR) / "logs" / "source-guard.err.log"),
    }
    data_dir_override = os.environ.get("WE_GROUPCHAT_OBSIDIAN_DATA_DIR")
    if data_dir_override:
        payload["EnvironmentVariables"] = {
            "WE_GROUPCHAT_OBSIDIAN_DATA_DIR": data_dir_override,
        }
    return payload


def _write_plist(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            plistlib.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        path.chmod(0o644)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True)


def install_agent(config: dict, *, load_now: bool = False, path: Path | None = None, runner=_run) -> int:
    path = path or agent_plist_path()
    plist = build_agent_plist(config)
    python = Path(plist["ProgramArguments"][0])
    if not python.is_file():
        raise SystemExit(f"项目 venv Python 不存在: {python}")
    (Path(DATA_DIR) / "logs").mkdir(parents=True, exist_ok=True)
    _write_plist(path, plist)
    print(f"Source guard LaunchAgent plist installed: {path}")
    if load_now:
        domain = f"gui/{os.getuid()}"
        runner(["launchctl", "bootout", domain, str(path)])
        result = runner(["launchctl", "bootstrap", domain, str(path)])
        if result.returncode != 0:
            raise SystemExit(result.stderr.strip() or "launchctl bootstrap failed")
        runner(["launchctl", "enable", f"{domain}/{SOURCE_GUARD_LAUNCH_AGENT_LABEL}"])
        print("Source guard LaunchAgent loaded.")
    else:
        print("Not loaded. Re-run with --load-now to activate the schedule.")
    return 0


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
    print(f"  agent installed: {plist.exists()}")
    print(f"  agent loaded: {agent.loaded}")
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
        print("Source guard enabled. This does not install or load the LaunchAgent.")
        return 0
    if args.command == "disable":
        _set_config("wechat_source_guard_enabled", False)
        print("Source guard disabled. The LaunchAgent installation state is unchanged.")
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
