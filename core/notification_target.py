"""Helpers for attaching local open targets to app notifications."""
from __future__ import annotations

import os
from urllib.parse import quote


def notification_data_for_path(path: str | os.PathLike[str] | None) -> dict | None:
    text = str(path or "").strip()
    if not text:
        return None
    return {"open_path": os.path.abspath(os.path.expanduser(text))}


def target_path_from_notification(data) -> str:
    if not isinstance(data, dict):
        return ""
    path = os.path.abspath(os.path.expanduser(str(data.get("open_path") or "").strip()))
    if not path or not os.path.exists(path):
        return ""
    return path


def obsidian_uri_for_path(path: str | os.PathLike[str] | None) -> str:
    text = str(path or "").strip()
    if not text:
        return ""
    absolute = os.path.abspath(os.path.expanduser(text))
    if not absolute.lower().endswith((".md", ".markdown")):
        return ""
    return "obsidian://open?path=" + quote(absolute, safe="/")


def notification_open_commands_for_path(path: str | os.PathLike[str] | None) -> list[list[str]]:
    text = str(path or "").strip()
    if not text:
        return []
    absolute = os.path.abspath(os.path.expanduser(text))
    commands = []
    obsidian_uri = obsidian_uri_for_path(absolute)
    if obsidian_uri:
        commands.append(["open", obsidian_uri])
    commands.append(["open", absolute])
    return commands
