"""macOS notification identity diagnostics."""
from __future__ import annotations

import plistlib
from pathlib import Path

from .project_identity import LAUNCH_AGENT_LABEL

EXPECTED_NOTIFICATION_BUNDLE_ID = LAUNCH_AGENT_LABEL
EXPECTED_NOTIFICATION_BUNDLE_NAME = "微信总结"
PYTHON_NOTIFICATION_BUNDLE_IDS = {"org.python.python"}


def _text(value) -> str:
    return "" if value is None else str(value)


def notification_identity_status(
    bundle=None,
    expected_bundle_identifier: str = EXPECTED_NOTIFICATION_BUNDLE_ID,
) -> dict:
    """Return the current macOS bundle identity used by user notifications.

    Source installs launched through a virtualenv commonly inherit
    ``org.python.python``. That can schedule notifications without giving the
    project a stable entry in macOS notification settings.
    """
    if bundle is None:
        try:
            from Foundation import NSBundle

            bundle = NSBundle.mainBundle()
        except Exception as exc:
            return {
                "ok": False,
                "bundle_identifier": "",
                "bundle_name": "",
                "bundle_path": "",
                "expected_bundle_identifier": expected_bundle_identifier,
                "message": f"notification identity unavailable: {type(exc).__name__}",
            }

    try:
        bundle_identifier = _text(bundle.bundleIdentifier())
        bundle_name = _text(bundle.objectForInfoDictionaryKey_("CFBundleName"))
        bundle_path = _text(bundle.bundlePath())
    except Exception as exc:
        return {
            "ok": False,
            "bundle_identifier": "",
            "bundle_name": "",
            "bundle_path": "",
            "expected_bundle_identifier": expected_bundle_identifier,
            "message": f"notification identity unreadable: {type(exc).__name__}",
        }

    ok = bundle_identifier == expected_bundle_identifier
    if ok:
        message = "notification identity is stable"
    elif bundle_identifier in PYTHON_NOTIFICATION_BUNDLE_IDS or bundle_name == "Python":
        message = "running under Python notification identity"
    elif not bundle_identifier:
        message = "missing notification bundle identifier"
    else:
        message = "notification bundle identifier does not match project identity"

    return {
        "ok": ok,
        "bundle_identifier": bundle_identifier,
        "bundle_name": bundle_name,
        "bundle_path": bundle_path,
        "expected_bundle_identifier": expected_bundle_identifier,
        "message": message,
        "source": "current-process",
    }


def app_bundle_for_executable(executable: str | Path) -> Path | None:
    """Return the nearest ``.app`` ancestor for a bundle executable path."""
    path = Path(executable)
    for candidate in (path, *path.parents):
        if candidate.suffix == ".app":
            return candidate
    return None


def notification_identity_status_for_app_bundle(
    app_bundle: str | Path,
    expected_bundle_identifier: str = EXPECTED_NOTIFICATION_BUNDLE_ID,
) -> dict:
    app_path = Path(app_bundle)
    plist_path = app_path / "Contents" / "Info.plist"
    try:
        with plist_path.open("rb") as handle:
            info = plistlib.load(handle)
    except Exception as exc:
        return {
            "ok": False,
            "bundle_identifier": "",
            "bundle_name": "",
            "bundle_path": str(app_path),
            "expected_bundle_identifier": expected_bundle_identifier,
            "message": f"app bundle identity unreadable: {type(exc).__name__}",
            "source": "launch-agent-app-bundle",
        }
    bundle_identifier = _text(info.get("CFBundleIdentifier"))
    bundle_name = _text(info.get("CFBundleDisplayName") or info.get("CFBundleName"))
    ok = bundle_identifier == expected_bundle_identifier
    return {
        "ok": ok,
        "bundle_identifier": bundle_identifier,
        "bundle_name": bundle_name,
        "bundle_path": str(app_path),
        "expected_bundle_identifier": expected_bundle_identifier,
        "message": "notification identity is stable"
        if ok
        else "notification bundle identifier does not match project identity",
        "source": "launch-agent-app-bundle",
    }


def notification_identity_status_for_launch_agent(record, current_bundle=None) -> dict:
    """Prefer the LaunchAgent app bundle identity, falling back to this process."""
    for arg in getattr(record, "program_arguments", ()) or ():
        app_bundle = app_bundle_for_executable(arg)
        if app_bundle:
            return notification_identity_status_for_app_bundle(app_bundle)
    status = notification_identity_status(bundle=current_bundle)
    status["source"] = "current-process"
    return status
