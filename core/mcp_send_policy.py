"""MCP send-message authority policy."""
from __future__ import annotations

VALID_SEND_MODES = {"disabled", "dry_run", "allowlist", "enabled"}


def _mode(config: dict) -> str:
    mode = str((config or {}).get("mcp_send_mode") or "disabled").strip().lower()
    return mode if mode in VALID_SEND_MODES else "disabled"


def check_send_policy(config: dict, text: str, chat_name: str, resolve_username=None) -> dict:
    """Return the action allowed by the local MCP send policy.

    Actions:
    - blocked: do not send
    - dry_run: report the send request without touching WeChat
    - send: caller may invoke the UI sender
    """
    mode = _mode(config)
    target = str(chat_name or "").strip()
    body = str(text or "").strip()

    if mode == "disabled":
        return {
            "action": "blocked",
            "mode": mode,
            "target": target,
            "username": "",
            "reason": "MCP sending is disabled",
        }
    if not target:
        return {
            "action": "blocked",
            "mode": mode,
            "target": target,
            "username": "",
            "reason": "MCP send target is required",
        }
    if not body:
        return {
            "action": "blocked",
            "mode": mode,
            "target": target,
            "username": "",
            "reason": "MCP send text is empty",
        }
    if mode == "dry_run":
        return {
            "action": "dry_run",
            "mode": mode,
            "target": target,
            "username": "",
            "reason": "MCP send dry run",
        }
    if mode == "allowlist":
        username = resolve_username(target) if resolve_username else target
        allowed = set(config.get("mcp_send_allowlist") or [])
        if username and username in allowed:
            return {
                "action": "send",
                "mode": mode,
                "target": target,
                "username": username,
                "reason": "target allowed",
            }
        return {
            "action": "blocked",
            "mode": mode,
            "target": target,
            "username": username or "",
            "reason": "target is not in mcp_send_allowlist",
        }

    return {
        "action": "send",
        "mode": mode,
        "target": target,
        "username": "",
        "reason": "sending enabled",
    }
