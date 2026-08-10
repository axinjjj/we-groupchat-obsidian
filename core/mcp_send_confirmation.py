"""Nonce-based confirmation state for MCP message sending."""
from __future__ import annotations

import secrets
import time

from .mcp_send_policy import check_send_policy


class SendConfirmationStore:
    """In-memory prepare/confirm store scoped to one MCP server process."""

    def __init__(self, ttl_seconds: int = 120, now_func=time.time, nonce_func=None):
        self.ttl_seconds = ttl_seconds
        self.now_func = now_func
        self.nonce_func = nonce_func or (lambda: secrets.token_urlsafe(18))
        self._pending: dict[str, dict] = {}

    def prepare(self, config: dict, text: str, chat_name: str, resolve_username=None) -> dict:
        decision = check_send_policy(
            config,
            text=text,
            chat_name=chat_name,
            resolve_username=resolve_username,
        )
        if decision["action"] != "send":
            return decision

        nonce = self.nonce_func()
        now = int(self.now_func())
        target = decision["target"]
        body = str(text or "").strip()
        item = {
            "nonce": nonce,
            "target": target,
            "text": body,
            "mode": decision["mode"],
            "username": decision.get("username", ""),
            "created_at": now,
            "expires_at": now + self.ttl_seconds,
        }
        self._pending[nonce] = item
        return {
            "action": "confirm_required",
            "nonce": nonce,
            "target": target,
            "username": item["username"],
            "text_preview": body,
            "expires_at": item["expires_at"],
            "reason": "confirmation required before real MCP send",
        }

    def confirm(self, nonce: str, text: str, chat_name: str, config: dict, send_func, resolve_username=None) -> dict:
        nonce = str(nonce or "").strip()
        pending = self._pending.get(nonce)
        if not pending:
            return {"action": "blocked", "reason": "confirmation nonce not found"}

        now = int(self.now_func())
        if now > int(pending["expires_at"]):
            self._pending.pop(nonce, None)
            return {"action": "blocked", "reason": "confirmation nonce expired"}

        target = str(chat_name or "").strip()
        body = str(text or "").strip()
        if target != pending["target"] or body != pending["text"]:
            return {"action": "blocked", "reason": "confirmation target or text changed"}

        decision = check_send_policy(
            config,
            text=body,
            chat_name=target,
            resolve_username=resolve_username,
        )
        if decision["action"] != "send":
            return {"action": "blocked", "reason": decision["reason"]}

        self._pending.pop(nonce, None)
        ok, message = send_func(body, target)
        if not ok:
            return {
                "action": "failed",
                "target": target,
                "message": message,
            }
        return {
            "action": "sent",
            "target": target,
            "message": message,
        }
