#!/usr/bin/env python3
"""Manage a per-user Windows logon task with bounded crash restart."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from xml.etree import ElementTree

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from core.keychain import delete_key
from core.config import ensure_private_file
from core.windows_key_extractor import WINDOWS_RAW_KEY_CREDENTIAL_ACCOUNT
from core.windows_console import configure_utf8_stdio
from core.windows_permissions import current_user_sid_string

TASK_NAME = r"\we-groupchat-obsidian"
TASK_XML_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def _subelement(parent, name, text=None, **attributes):
    element = ElementTree.SubElement(
        parent,
        f"{{{TASK_XML_NAMESPACE}}}{name}",
        attributes,
    )
    if text is not None:
        element.text = str(text)
    return element


def task_action(project_dir: Path = PROJECT_DIR, *, environ=None) -> tuple[str, str]:
    project_dir = Path(project_dir).resolve()
    launcher = project_dir / "launchers" / "启动.ps1"
    if not launcher.is_file():
        raise RuntimeError(f"Windows 启动器不存在: {launcher}")
    environ = os.environ if environ is None else environ
    system_root = str(environ.get("SystemRoot") or r"C:\Windows")
    powershell = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    arguments = subprocess.list2cmdline([
        "-NoProfile",
        "-WindowStyle",
        "Hidden",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(launcher),
        "--autostart",
        "--no-pause",
    ])
    return str(powershell), arguments


def build_task_xml(
    *,
    project_dir: Path = PROJECT_DIR,
    user_sid: str,
    environ=None,
) -> bytes:
    """Build the Task Scheduler definition without embedding any credential."""
    project_dir = Path(project_dir).resolve()
    command, arguments = task_action(project_dir, environ=environ)
    ElementTree.register_namespace("", TASK_XML_NAMESPACE)
    task = ElementTree.Element(
        f"{{{TASK_XML_NAMESPACE}}}Task",
        {"version": "1.4"},
    )
    registration = _subelement(task, "RegistrationInfo")
    _subelement(
        registration,
        "Description",
        "Start the WGO tray app at logon and restart it after bounded failures.",
    )

    triggers = _subelement(task, "Triggers")
    logon = _subelement(triggers, "LogonTrigger")
    _subelement(logon, "Enabled", "true")
    _subelement(logon, "UserId", user_sid)

    principals = _subelement(task, "Principals")
    principal = _subelement(principals, "Principal", id="Author")
    _subelement(principal, "UserId", user_sid)
    _subelement(principal, "LogonType", "InteractiveToken")
    _subelement(principal, "RunLevel", "LeastPrivilege")

    settings = _subelement(task, "Settings")
    _subelement(settings, "MultipleInstancesPolicy", "IgnoreNew")
    _subelement(settings, "DisallowStartIfOnBatteries", "false")
    _subelement(settings, "StopIfGoingOnBatteries", "false")
    _subelement(settings, "AllowHardTerminate", "true")
    _subelement(settings, "StartWhenAvailable", "true")
    _subelement(settings, "RunOnlyIfNetworkAvailable", "false")
    idle = _subelement(settings, "IdleSettings")
    _subelement(idle, "StopOnIdleEnd", "false")
    _subelement(idle, "RestartOnIdle", "false")
    _subelement(settings, "AllowStartOnDemand", "true")
    _subelement(settings, "Enabled", "true")
    _subelement(settings, "Hidden", "false")
    _subelement(settings, "RunOnlyIfIdle", "false")
    _subelement(settings, "WakeToRun", "false")
    _subelement(settings, "ExecutionTimeLimit", "PT0S")
    _subelement(settings, "Priority", "7")
    restart = _subelement(settings, "RestartOnFailure")
    _subelement(restart, "Interval", "PT1M")
    _subelement(restart, "Count", "3")

    actions = _subelement(task, "Actions", Context="Author")
    execute = _subelement(actions, "Exec")
    _subelement(execute, "Command", command)
    _subelement(execute, "Arguments", arguments)
    _subelement(execute, "WorkingDirectory", str(project_dir))
    return ElementTree.tostring(task, encoding="utf-16", xml_declaration=True)


def _run_schtasks(arguments, *, runner=subprocess.run):
    return runner(
        ["schtasks.exe", *arguments],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def _register_task_definition(task_name, xml, *, runner=subprocess.run) -> int:
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as handle:
            temp_path = handle.name
            handle.write(xml)
            handle.flush()
            os.fsync(handle.fileno())
        ensure_private_file(temp_path)
        result = _run_schtasks(
            ["/Create", "/TN", task_name, "/XML", temp_path, "/F"],
            runner=runner,
        )
        if result.returncode != 0:
            raise RuntimeError(f"scheduled_task_create_failed:{result.returncode}")
        return 0
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def install(
    *,
    project_dir: Path = PROJECT_DIR,
    user_sid: str | None = None,
    runner=subprocess.run,
) -> int:
    xml = build_task_xml(
        project_dir=project_dir,
        user_sid=user_sid or current_user_sid_string(),
    )
    _register_task_definition(TASK_NAME, xml, runner=runner)
    print("已安装微信总结 Windows 登录自启与异常保活。")
    return 0


def uninstall(*, runner=subprocess.run) -> int:
    query = _run_schtasks(["/Query", "/TN", TASK_NAME], runner=runner)
    if query.returncode == 0:
        result = _run_schtasks(["/Delete", "/TN", TASK_NAME, "/F"], runner=runner)
        if result.returncode != 0:
            raise RuntimeError(f"scheduled_task_delete_failed:{result.returncode}")
        print("已卸载微信总结 Windows 登录自启与异常保活。")
    else:
        print("未找到微信总结 Windows 登录自启。")
    delete_key(WINDOWS_RAW_KEY_CREDENTIAL_ACCOUNT)
    return 0


def status(*, runner=subprocess.run) -> int:
    result = _run_schtasks(["/Query", "/TN", TASK_NAME, "/FO", "LIST"], runner=runner)
    print(f"Windows 登录自启/保活: {'已安装' if result.returncode == 0 else '未安装'}")
    return 0 if result.returncode == 0 else 1


def validate(*, runner=subprocess.run) -> int:
    """Register/query/delete an inert-until-next-logon task under a random name."""
    task_name = f"{TASK_NAME}-validation-{uuid.uuid4().hex}"
    if not task_name.startswith(f"{TASK_NAME}-validation-"):
        raise RuntimeError("validation_task_name_rejected")
    xml = build_task_xml(user_sid=current_user_sid_string())
    created = False
    try:
        _register_task_definition(task_name, xml, runner=runner)
        created = True
        query = _run_schtasks(["/Query", "/TN", task_name], runner=runner)
        if query.returncode != 0:
            raise RuntimeError(f"scheduled_task_query_failed:{query.returncode}")
    finally:
        if created:
            deleted = _run_schtasks(["/Delete", "/TN", task_name, "/F"], runner=runner)
            if deleted.returncode != 0:
                raise RuntimeError(f"scheduled_task_cleanup_failed:{deleted.returncode}")
    print("Windows 计划任务 XML 注册/查询/清理验证通过。")
    return 0


def main() -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="Manage Windows per-user autostart.")
    parser.add_argument("action", choices=("install", "uninstall", "status", "validate"))
    args = parser.parse_args()
    if args.action == "install":
        return install()
    if args.action == "uninstall":
        return uninstall()
    if args.action == "status":
        return status()
    return validate()


if __name__ == "__main__":
    raise SystemExit(main())
