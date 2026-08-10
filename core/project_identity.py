"""Stable project identity constants for runtime, docs, and integrations."""
from __future__ import annotations

from pathlib import Path

PROJECT_SLUG = "we-groupchat-obsidian"
LEGACY_PROJECT_SLUGS = ("wechat-summary", "mac-wechat-summary")

DATA_DIR_NAME = f".{PROJECT_SLUG}"
LEGACY_DATA_DIR_NAME = ".wechat-summary"

MCP_SERVER_ID = PROJECT_SLUG
LEGACY_MCP_SERVER_ID = "wechat-summary"

KEYCHAIN_SERVICE_NAME = PROJECT_SLUG
LEGACY_KEYCHAIN_SERVICE_NAMES = ("wechat-summary",)

LAUNCH_AGENT_LABEL = f"io.github.indeliblevivi.{PROJECT_SLUG}"
LEGACY_RUNTIME_DIR_NAMES = ("mac-wechat-summary",)


def data_dir(home: str | Path | None = None) -> Path:
    root = Path(home).expanduser() if home else Path.home()
    return root / DATA_DIR_NAME


def legacy_data_dir(home: str | Path | None = None) -> Path:
    root = Path(home).expanduser() if home else Path.home()
    return root / LEGACY_DATA_DIR_NAME
