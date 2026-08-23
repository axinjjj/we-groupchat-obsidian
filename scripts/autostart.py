#!/usr/bin/env python3
"""Install or uninstall the macOS LaunchAgent for we-groupchat-obsidian."""
from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from core.launch_agent import (
    DEFAULT_LABEL,
    choose_install_record,
    default_launch_agent_record,
    discover_managed_launch_agents,
    launch_agent_status,
)
from core.config import DATA_DIR

LOG_DIR = Path(DATA_DIR) / "logs"


def run(args: list[str], check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=check)


def bootout_if_loaded(plist_path: Path) -> None:
    run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist_path)])


def bootstrap(label: str, plist_path: Path) -> None:
    result = run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)])
    if result.returncode != 0 and "service already loaded" not in result.stderr.lower():
        raise SystemExit(result.stderr.strip() or "launchctl bootstrap failed")
    run(["launchctl", "enable", f"gui/{os.getuid()}/{label}"])


def app_bundle_executable(app_bundle: str | os.PathLike[str]) -> Path:
    app_path = Path(app_bundle).expanduser().resolve()
    plist_path = app_path / "Contents" / "Info.plist"
    try:
        with plist_path.open("rb") as handle:
            info = plistlib.load(handle)
    except OSError as exc:
        raise SystemExit(f"无法读取 app bundle Info.plist: {plist_path} ({exc})")
    executable_name = str(info.get("CFBundleExecutable") or "").strip()
    if not executable_name:
        raise SystemExit(f"app bundle 缺少 CFBundleExecutable: {plist_path}")
    executable = app_path / "Contents" / "MacOS" / executable_name
    if not executable.exists():
        raise SystemExit(f"app bundle executable 不存在: {executable}")
    return executable


def build_plist(
    label: str,
    app_bundle: str | os.PathLike[str] | None = None,
    project_dir: str | os.PathLike[str] = PROJECT_DIR,
) -> dict:
    project_path = Path(project_dir).resolve()
    if app_bundle:
        program_arguments = [str(app_bundle_executable(app_bundle)), "--autostart"]
    else:
        program_arguments = [
            "/bin/bash",
            str(project_path / "启动.command"),
            "--autostart",
        ]
    return {
        "Label": label,
        "ProgramArguments": program_arguments,
        "WorkingDirectory": str(project_path),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 300,
        "StandardOutPath": str(LOG_DIR / "autostart.out.log"),
        "StandardErrorPath": str(LOG_DIR / "autostart.err.log"),
        "EnvironmentVariables": {
            "WE_GROUPCHAT_OBSIDIAN_NO_PAUSE": "1",
            "WE_GROUPCHAT_OBSIDIAN_AUTOSTART": "1",
        },
    }


def install(
    load_now: bool = False,
    migrate_label: bool = False,
    app_bundle: str | os.PathLike[str] | None = None,
) -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    target = choose_install_record(PROJECT_DIR, migrate_label=migrate_label)
    target.plist_path.parent.mkdir(parents=True, exist_ok=True)
    if migrate_label:
        for record in discover_managed_launch_agents(PROJECT_DIR, include_related=True):
            if record.label != DEFAULT_LABEL:
                bootout_if_loaded(record.plist_path)
                try:
                    record.plist_path.unlink()
                except FileNotFoundError:
                    pass
    with target.plist_path.open("wb") as f:
        plistlib.dump(build_plist(target.label, app_bundle=app_bundle), f)

    if load_now:
        bootout_if_loaded(target.plist_path)
        bootstrap(target.label, target.plist_path)

    print("已安装微信总结登录自启")
    print(f"  label: {target.label}")
    print(f"  plist: {target.plist_path}")
    print(f"  logs:  {LOG_DIR}")
    if app_bundle:
        print(f"  app:   {Path(app_bundle).expanduser().resolve()}")
    if target.label != DEFAULT_LABEL:
        print(f"  保留现有 LaunchAgent label；如需迁移到默认 label，运行：./launchers/安装自动启动.command --migrate-label")
    if load_now:
        print("  LaunchAgent 已加载；下次登录也会自动启动。")
    else:
        print("  已写入 LaunchAgent；下次登录会自动启动。")
        print("  如需现在立刻加载，运行：./launchers/安装自动启动.command --load-now")
    return 0


def uninstall() -> int:
    records = discover_managed_launch_agents(PROJECT_DIR, include_related=True)
    if not records:
        print("未找到当前项目的微信总结 LaunchAgent")
        return 0
    for record in records:
        bootout_if_loaded(record.plist_path)
        if record.plist_path.exists():
            record.plist_path.unlink()
        print(f"  removed: {record.plist_path}")
    print("已卸载微信总结登录自启")
    return 0


def status() -> int:
    records = discover_managed_launch_agents(PROJECT_DIR, include_related=True)
    if not records:
        records = [default_launch_agent_record(PROJECT_DIR)]
    print(f"default label: {DEFAULT_LABEL}")
    for record in records:
        status = launch_agent_status(record.label)
        print(f"label: {record.label}")
        print(f"plist: {record.plist_path} ({'exists' if record.plist_path.exists() else 'missing'})")
        print(f"match: {record.match_kind}")
        print(f"loaded: {status.loaded}")
        print(f"running: {status.running}")
        detail = status.state or status.job_state
        if detail:
            print(f"state: {detail}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage we-groupchat-obsidian macOS autostart.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    install_parser = sub.add_parser("install")
    install_parser.add_argument("--load-now", action="store_true", help="Load the LaunchAgent immediately after writing the plist.")
    install_parser.add_argument("--migrate-label", action="store_true", help="Move an existing managed plist to the neutral default label.")
    install_parser.add_argument("--app-bundle", help="Run this .app bundle executable instead of 启动.command.")
    sub.add_parser("uninstall")
    sub.add_parser("status")
    args = parser.parse_args()

    if args.cmd == "install":
        return install(load_now=args.load_now, migrate_label=args.migrate_label, app_bundle=args.app_bundle)
    if args.cmd == "uninstall":
        return uninstall()
    if args.cmd == "status":
        return status()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
