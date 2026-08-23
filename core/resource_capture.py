"""Deterministic selected-chat capture for link and file resource occurrences.

This module deliberately separates source capture from every backup transport.
A source cursor advances only in the same SQLite transaction that durably records
all link/file occurrences found in that source page.  File bytes are resolved
later through the shared AttachmentArchive CAS; link identity is the exact URL
string observed in the WeChat message, not an AI-produced summary.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import sqlite3
import time
import uuid
from datetime import datetime

from .attachment_archive import AttachmentArchive
from .config import (
    DATA_DIR,
    active_monitor_chats,
    selected_resource_backup_chats,
)
from .link_preview import URL_RE
from .wechat_db import WeChatSourceDegraded


SCHEMA_VERSION = 1
LINK_ID_DOMAIN = b"we-groupchat-resource-link-v1\0"
CHAT_ID_DOMAIN = "we-groupchat-resource-chat-v1\0"
RETRYABLE_FILE_STATES = (
    "waiting_cache",
    "insufficient_local_space",
    "retry_wait",
)


class ResourceCaptureError(RuntimeError):
    """Privacy-safe resource-capture failure with a stable error code."""

    def __init__(self, code: str):
        super().__init__(str(code))
        self.code = str(code)


def _safe_label(value, fallback="未命名群聊", limit=180):
    text = "".join(" " if ord(char) < 32 else char for char in str(value or ""))
    text = text.replace("/", "／").replace(":", "：").strip().strip(".")
    text = re.sub(r"\s+", " ", text)
    return (text[:limit] or fallback)


def _month(timestamp):
    return datetime.fromtimestamp(float(timestamp)).strftime("%Y-%m")


def _exact_links(text):
    """Return exact HTTP(S) strings in source order, deduplicated byte-for-byte."""
    links = []
    seen = set()
    for match in URL_RE.finditer(str(text or "")):
        url = match.group(0)
        if url and url not in seen:
            seen.add(url)
            links.append(url)
    return links


def _link_title(text, url):
    """Recover the title from WeChat's deterministic ``[链接] title URL`` form."""
    value = str(text or "").strip()
    if not value.startswith("[链接]"):
        return ""
    value = value[len("[链接]"):].strip()
    position = value.find(url)
    if position >= 0:
        value = value[:position].strip()
    return _safe_label(value, "", limit=180)


def _url_sha256(url):
    return hashlib.sha256(LINK_ID_DOMAIN + str(url).encode("utf-8")).hexdigest()


def eligible_selected_chats(config):
    """Return only chats that are both monitored and explicitly backup-selected.

    The intersection is the external-disclosure boundary.  A chat selected in an
    obsolete Drive configuration but no longer active in monitor configuration is
    intentionally excluded.
    """
    config = config if isinstance(config, dict) else {}
    active = {
        str(chat.get("username") or "").strip(): str(chat.get("name") or "").strip()
        for chat in active_monitor_chats(config)
        if str(chat.get("username") or "").strip()
    }
    aliases = config.get("monitor_chat_aliases")
    aliases = aliases if isinstance(aliases, dict) else {}
    result = []
    for index, selected in enumerate(selected_resource_backup_chats(config), 1):
        username = str(selected.get("username") or "").strip()
        if username not in active:
            continue
        fallback = f"未命名群聊 {index}"
        alias = ""
        for candidate in (
            selected.get("alias"), aliases.get(username), active[username]
        ):
            value = str(candidate or "").strip()
            if value and "@chatroom" not in value.casefold():
                alias = value
                break
        result.append({
            "username": username,
            "alias": _safe_label(alias, fallback),
            "selected_since": max(0, int(selected.get("selected_since") or 0)),
        })
    return result


def resource_backup_chat_candidates(config):
    """Return active chats in stable config order with privacy-safe labels."""
    config = config if isinstance(config, dict) else {}
    aliases = config.get("monitor_chat_aliases")
    aliases = aliases if isinstance(aliases, dict) else {}
    result = []
    for chat in active_monitor_chats(config):
        username = str(chat.get("username") or "").strip()
        if not username:
            continue
        fallback = f"未命名群聊 {len(result) + 1}"
        alias = (
            str(aliases.get(username) or "").strip()
            or str(chat.get("name") or "").strip()
        )
        if not alias or alias.endswith("@chatroom"):
            alias = fallback
        result.append({"username": username, "alias": _safe_label(alias, fallback)})
    return result


class SelectedResourceCapture:
    """Capture selected-chat link/file occurrences into a provider-neutral ledger."""

    def __init__(
        self,
        config,
        *,
        source=None,
        now_func=time.time,
        random_func=random.random,
        archive_id_factory=None,
    ):
        self.config = dict(config or {})
        self.source = source
        self.now_func = now_func
        self.random_func = random_func
        self.archive_id_factory = archive_id_factory or (lambda: str(uuid.uuid4()))
        self.db_path = os.path.abspath(os.path.expanduser(
            self.config.get("resource_capture_db")
            or os.path.join(DATA_DIR, "resource_capture.db")
        ))
        self.archive_root = os.path.abspath(os.path.expanduser(
            self.config.get("attachment_archive_root")
            or os.path.join(DATA_DIR, "attachment_archive")
        ))
        self.archive = AttachmentArchive(
            self.config.get("monitor_knowledge_db") or os.path.join(DATA_DIR, "monitor_knowledge.db"),
            self.archive_root,
            db_dir=self.config.get("db_dir") or "",
            archive_kinds=("file",),
            max_object_bytes=self.config.get("attachment_archive_max_object_bytes", 512 * 1024 * 1024),
            min_free_bytes=self.config.get("attachment_archive_min_free_bytes", 1024 * 1024 * 1024),
            retry_base_seconds=self.config.get("attachment_archive_retry_base_seconds", 300),
            retry_max_seconds=self.config.get("attachment_archive_retry_max_seconds", 6 * 60 * 60),
            now_func=now_func,
        )
        self._ensure_schema()
        self.archive_id = self._ensure_archive_id()

    @classmethod
    def from_config(cls, config, **kwargs):
        return cls(config, **kwargs)

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _ensure_schema(self):
        os.makedirs(os.path.dirname(self.db_path), mode=0o700, exist_ok=True)
        try:
            os.chmod(os.path.dirname(self.db_path), 0o700)
        except OSError:
            pass
        conn = self._connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS resource_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS resource_chats (
                    chat_username TEXT PRIMARY KEY,
                    chat_key TEXT NOT NULL,
                    chat_alias TEXT NOT NULL,
                    start_timestamp INTEGER NOT NULL,
                    selected_since INTEGER NOT NULL DEFAULT 0,
                    selection_epoch INTEGER NOT NULL DEFAULT 1,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS resource_shards (
                    chat_username TEXT NOT NULL,
                    source_shard_id TEXT NOT NULL,
                    cursor_timestamp INTEGER NOT NULL,
                    cursor_message_ids_json TEXT NOT NULL DEFAULT '[]',
                    source_state TEXT NOT NULL DEFAULT 'healthy',
                    last_error_code TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(chat_username, source_shard_id),
                    FOREIGN KEY(chat_username) REFERENCES resource_chats(chat_username)
                );

                CREATE TABLE IF NOT EXISTS resource_occurrences (
                    occurrence_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_username TEXT NOT NULL,
                    chat_key TEXT NOT NULL,
                    chat_alias TEXT NOT NULL,
                    source_message_id TEXT NOT NULL,
                    resource_index INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    source_timestamp INTEGER NOT NULL,
                    source_month TEXT NOT NULL,
                    source_time TEXT NOT NULL DEFAULT '',
                    source_sender TEXT NOT NULL DEFAULT '',
                    original_name TEXT NOT NULL DEFAULT '',
                    observed_url TEXT NOT NULL DEFAULT '',
                    url_sha256 TEXT NOT NULL DEFAULT '',
                    declared_size INTEGER,
                    declared_hash TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    resolution_method TEXT NOT NULL DEFAULT '',
                    object_sha256 TEXT NOT NULL DEFAULT '',
                    object_size INTEGER,
                    object_relpath TEXT NOT NULL DEFAULT '',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_retry_at REAL NOT NULL DEFAULT 0,
                    last_error_code TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(chat_username, source_message_id, kind, resource_index),
                    CHECK(kind IN ('link', 'file'))
                );
                CREATE INDEX IF NOT EXISTS idx_resource_occurrences_chat_time
                    ON resource_occurrences(chat_key, source_timestamp, occurrence_id);
                CREATE INDEX IF NOT EXISTS idx_resource_occurrences_status
                    ON resource_occurrences(status, next_retry_at, occurrence_id);
                CREATE INDEX IF NOT EXISTS idx_resource_occurrences_object
                    ON resource_occurrences(object_sha256, occurrence_id);
                """
            )
            columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(resource_chats)")
            }
            if "selected_since" not in columns:
                conn.execute(
                    "ALTER TABLE resource_chats "
                    "ADD COLUMN selected_since INTEGER NOT NULL DEFAULT 0"
                )
            if "selection_epoch" not in columns:
                conn.execute(
                    "ALTER TABLE resource_chats "
                    "ADD COLUMN selection_epoch INTEGER NOT NULL DEFAULT 1"
                )
            conn.commit()
        finally:
            conn.close()
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass

    def _meta_get(self, key, default=""):
        conn = self._connect()
        try:
            row = conn.execute("SELECT value FROM resource_meta WHERE key = ?", (key,)).fetchone()
            return str(row["value"]) if row else default
        finally:
            conn.close()

    def _meta_set(self, key, value):
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO resource_meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
            conn.commit()
        finally:
            conn.close()

    def _ensure_archive_id(self):
        value = self._meta_get("archive_id")
        if value:
            return value
        value = str(self.archive_id_factory())
        try:
            value = str(uuid.UUID(value))
        except ValueError as exc:
            raise ResourceCaptureError("archive_id_invalid") from exc
        self._meta_set("archive_id", value)
        return value

    def _chat_key(self, username):
        return hashlib.sha256(
            f"{CHAT_ID_DOMAIN}{self.archive_id}\0{username}".encode("utf-8")
        ).hexdigest()

    def selected_chats(self):
        return [
            {
                "username": chat["username"],
                "alias": chat["alias"],
                "chat_key": self._chat_key(chat["username"]),
                "selected_since": int(chat.get("selected_since") or 0),
            }
            for chat in eligible_selected_chats(self.config)
        ]

    def selected_chat_keys(self):
        return [chat["chat_key"] for chat in self.selected_chats()]

    def initialize_selected_chat_cursors(self, start_timestamp=None):
        default_start = int(
            self.now_func() if start_timestamp is None else start_timestamp
        )
        chats = self.selected_chats()
        inserted = 0
        reselected = 0
        conn = self._connect()
        try:
            for chat in chats:
                existing = conn.execute(
                    "SELECT * FROM resource_chats WHERE chat_username = ?",
                    (chat["username"],),
                ).fetchone()
                selected_since = int(chat.get("selected_since") or 0)
                start = (
                    default_start
                    if start_timestamp is not None or not selected_since
                    else selected_since
                )
                if existing is None:
                    conn.execute(
                        """
                        INSERT INTO resource_chats(
                            chat_username, chat_key, chat_alias, start_timestamp,
                            selected_since, selection_epoch, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 1, ?)
                        """,
                        (
                            chat["username"], chat["chat_key"], chat["alias"],
                            start, selected_since, self.now_func(),
                        ),
                    )
                    inserted += 1
                else:
                    epoch_changed = bool(
                        selected_since
                        and int(existing["selected_since"] or 0) != selected_since
                    )
                    if epoch_changed:
                        conn.execute(
                            """
                            UPDATE resource_chats
                            SET chat_key = ?, chat_alias = ?, start_timestamp = ?,
                                selected_since = ?, selection_epoch = selection_epoch + 1,
                                updated_at = ?
                            WHERE chat_username = ?
                            """,
                            (
                                chat["chat_key"], chat["alias"], start,
                                selected_since, self.now_func(), chat["username"],
                            ),
                        )
                        conn.execute(
                            "DELETE FROM resource_shards WHERE chat_username = ?",
                            (chat["username"],),
                        )
                        reselected += 1
                    else:
                        conn.execute(
                            """
                            UPDATE resource_chats
                            SET chat_key = ?, chat_alias = ?, updated_at = ?
                            WHERE chat_username = ?
                            """,
                            (
                                chat["chat_key"], chat["alias"], self.now_func(),
                                chat["username"],
                            ),
                        )
                conn.execute(
                    """
                    UPDATE resource_occurrences
                    SET chat_key = ?, chat_alias = ?, updated_at = ?
                    WHERE chat_username = ?
                    """,
                    (chat["chat_key"], chat["alias"], self.now_func(), chat["username"]),
                )
            conn.commit()
        finally:
            conn.close()
        return {
            "state": "initialized",
            "selected_chats": len(chats),
            "new_chats": inserted,
            "reselected_chats": reselected,
            "start_timestamp": default_start,
        }

    def _chat_row(self, username):
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT * FROM resource_chats WHERE chat_username = ?", (username,)
            ).fetchone()
        finally:
            conn.close()

    def _shard_state(self, chat_row, source_shard_id):
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO resource_shards(
                    chat_username, source_shard_id, cursor_timestamp,
                    cursor_message_ids_json, source_state, last_error_code, updated_at
                ) VALUES (?, ?, ?, '[]', 'healthy', '', ?)
                """,
                (
                    chat_row["chat_username"], source_shard_id,
                    int(chat_row["start_timestamp"]), self.now_func(),
                ),
            )
            row = conn.execute(
                """
                SELECT * FROM resource_shards
                WHERE chat_username = ? AND source_shard_id = ?
                """,
                (chat_row["chat_username"], source_shard_id),
            ).fetchone()
            conn.commit()
            return row
        finally:
            conn.close()

    @staticmethod
    def _source_error_code(exc):
        code = str(getattr(exc, "code", "") or "")
        if code in {
            "source_shard_unavailable",
            "source_shard_unknown",
            "source_shards_unavailable",
        }:
            return code
        return "source_shard_unavailable"

    def _mark_shard_degraded(self, username, source_shard_id, code):
        conn = self._connect()
        try:
            conn.execute(
                """
                UPDATE resource_shards
                SET source_state = 'source_degraded', last_error_code = ?, updated_at = ?
                WHERE chat_username = ? AND source_shard_id = ?
                """,
                (code, self.now_func(), username, source_shard_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _source_page(self, username, source_shard_id, cursor_timestamp, seen_ids, limit):
        if self.source is None:
            raise ResourceCaptureError("source_unavailable")
        request_limit = max(1, int(limit)) + len(seen_ids)
        messages = self.source.get_messages_for_shard(
            username,
            source_shard_id,
            since_ts=max(0, int(cursor_timestamp)),
            limit=request_limit,
            page_forward=True,
            since_inclusive=True,
        )
        fresh = []
        for message in messages:
            timestamp = int(message.get("timestamp") or 0)
            identity = str(message.get("source_message_id") or "")
            if not identity or timestamp < cursor_timestamp:
                continue
            if timestamp == cursor_timestamp and identity in seen_ids:
                continue
            fresh.append(message)
        fresh.sort(key=lambda item: (
            int(item.get("timestamp") or 0),
            str(item.get("source_message_id") or ""),
        ))
        return fresh[:limit]

    @staticmethod
    def _cursor_after(messages, old_timestamp, old_ids):
        if not messages:
            return old_timestamp, set(old_ids)
        newest = max(int(message.get("timestamp") or 0) for message in messages)
        identities = {
            str(message.get("source_message_id") or "")
            for message in messages
            if int(message.get("timestamp") or 0) == newest
        }
        if newest == old_timestamp:
            identities.update(old_ids)
        identities.discard("")
        return newest, identities

    @staticmethod
    def _inserted(cursor):
        return max(0, int(cursor.rowcount or 0))

    def _insert_occurrences(self, conn, chat, messages, now):
        captured_links = 0
        captured_files = 0
        for message in messages:
            source_message_id = str(message.get("source_message_id") or "").strip()
            timestamp = int(message.get("timestamp") or 0)
            if not source_message_id or not timestamp:
                continue
            source_time = str(message.get("time_str") or "")[:40]
            sender = str(message.get("sender") or message.get("group_nickname") or "")[:80]
            month = _month(timestamp)
            text = str(message.get("text") or message.get("content") or "")

            for index, url in enumerate(_exact_links(text)):
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO resource_occurrences(
                        chat_username, chat_key, chat_alias, source_message_id,
                        resource_index, kind, source_timestamp, source_month,
                        source_time, source_sender, original_name, observed_url,
                        url_sha256, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'link', ?, ?, ?, ?, ?, ?, ?,
                              'ready_metadata', ?, ?)
                    """,
                    (
                        chat["username"], chat["chat_key"], chat["alias"],
                        source_message_id, index, timestamp, month, source_time,
                        sender, _link_title(text, url), url, _url_sha256(url), now, now,
                    ),
                )
                captured_links += self._inserted(cursor)

            for fallback_index, resource in enumerate(message.get("resources") or []):
                if not isinstance(resource, dict) or str(resource.get("kind") or "") != "file":
                    continue
                try:
                    resource_index = max(0, int(resource.get("resource_index", fallback_index)))
                except (TypeError, ValueError):
                    resource_index = fallback_index
                declared_size = resource.get("declared_size")
                if declared_size is not None:
                    try:
                        declared_size = max(0, int(declared_size))
                    except (TypeError, ValueError):
                        declared_size = None
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO resource_occurrences(
                        chat_username, chat_key, chat_alias, source_message_id,
                        resource_index, kind, source_timestamp, source_month,
                        source_time, source_sender, original_name, declared_size,
                        declared_hash, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'file', ?, ?, ?, ?, ?, ?, ?,
                              'queued', ?, ?)
                    """,
                    (
                        chat["username"], chat["chat_key"], chat["alias"],
                        source_message_id, resource_index, timestamp, month,
                        source_time, sender,
                        _safe_label(resource.get("original_name"), "attachment"),
                        declared_size,
                        str(resource.get("declared_hash") or resource.get("md5") or "").lower()[:128],
                        now, now,
                    ),
                )
                captured_files += self._inserted(cursor)
        return captured_links, captured_files

    def scan(self):
        chats = self.selected_chats()
        if not chats:
            return {
                "state": "no_selected_chats",
                "scanned": 0,
                "captured_links": 0,
                "captured_files": 0,
            }
        if self.source is None:
            return {
                "state": "source_unavailable",
                "scanned": 0,
                "captured_links": 0,
                "captured_files": 0,
                "error_code": "source_unavailable",
            }
        initialized = self.initialize_selected_chat_cursors()["new_chats"]
        max_messages = max(1, int(
            self.config.get("resource_backup_max_messages_per_scan", 500)
        ))
        scanned = 0
        captured_links = 0
        captured_files = 0
        degraded_shards = 0
        source_error_code = ""

        for chat in chats:
            chat_row = self._chat_row(chat["username"])
            if chat_row is None:
                raise ResourceCaptureError("chat_state_missing")
            try:
                source_shards = list(self.source.get_message_shards(chat["username"]))
            except WeChatSourceDegraded as exc:
                degraded_shards += 1
                source_error_code = self._source_error_code(exc)
                continue
            if not source_shards:
                degraded_shards += 1
                source_error_code = "source_shards_unavailable"
                continue

            for source_shard_id in source_shards:
                shard = self._shard_state(chat_row, source_shard_id)
                cursor_timestamp = int(shard["cursor_timestamp"])
                try:
                    seen_ids = set(json.loads(shard["cursor_message_ids_json"] or "[]"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    seen_ids = set()
                try:
                    messages = self._source_page(
                        chat["username"], source_shard_id,
                        cursor_timestamp, seen_ids, max_messages,
                    )
                except WeChatSourceDegraded as exc:
                    code = self._source_error_code(exc)
                    self._mark_shard_degraded(chat["username"], source_shard_id, code)
                    degraded_shards += 1
                    source_error_code = code
                    continue

                new_timestamp, new_ids = self._cursor_after(
                    messages, cursor_timestamp, seen_ids
                )
                now = self.now_func()
                conn = self._connect()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    links, files = self._insert_occurrences(conn, chat, messages, now)
                    conn.execute(
                        """
                        UPDATE resource_shards
                        SET cursor_timestamp = ?, cursor_message_ids_json = ?,
                            source_state = 'healthy', last_error_code = '', updated_at = ?
                        WHERE chat_username = ? AND source_shard_id = ?
                        """,
                        (
                            new_timestamp, json.dumps(sorted(new_ids)), now,
                            chat["username"], source_shard_id,
                        ),
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    conn.close()
                captured_links += links
                captured_files += files
                scanned += len(messages)

        self._meta_set("source_state", "source_degraded" if degraded_shards else "healthy")
        self._meta_set("source_error_code", source_error_code)
        self._meta_set("last_scan_at", self.now_func())
        return {
            "state": "source_degraded" if degraded_shards else "healthy",
            "scanned": scanned,
            "captured_links": captured_links,
            "captured_files": captured_files,
            "initialized_chats": initialized,
            "degraded_shards": degraded_shards,
            "error_code": source_error_code,
        }

    def backfill(self, from_timestamp, *, apply=False):
        """Plan or explicitly apply historical occurrences without moving live cursors."""
        chats = self.selected_chats()
        if not chats:
            return {
                "state": "no_selected_chats",
                "scanned": 0,
                "discovered_links": 0,
                "discovered_files": 0,
                "inserted_links": 0,
                "inserted_files": 0,
            }
        if self.source is None:
            return {
                "state": "source_unavailable",
                "scanned": 0,
                "discovered_links": 0,
                "discovered_files": 0,
                "inserted_links": 0,
                "inserted_files": 0,
                "error_code": "source_unavailable",
            }
        self.initialize_selected_chat_cursors()
        max_messages = max(1, int(
            self.config.get("resource_backup_max_messages_per_scan", 500)
        ))
        scanned = 0
        discovered_links = 0
        discovered_files = 0
        inserted_links = 0
        inserted_files = 0
        degraded_shards = 0
        source_error_code = ""
        for chat in chats:
            try:
                source_shards = list(self.source.get_message_shards(chat["username"]))
            except WeChatSourceDegraded as exc:
                degraded_shards += 1
                source_error_code = self._source_error_code(exc)
                continue
            if not source_shards:
                degraded_shards += 1
                source_error_code = "source_shards_unavailable"
                continue
            for source_shard_id in source_shards:
                cursor_timestamp = max(0, int(from_timestamp))
                seen_ids = set()
                while True:
                    try:
                        page = self._source_page(
                            chat["username"], source_shard_id,
                            cursor_timestamp, seen_ids, max_messages,
                        )
                    except WeChatSourceDegraded as exc:
                        degraded_shards += 1
                        source_error_code = self._source_error_code(exc)
                        break
                    if not page:
                        break
                    scanned += len(page)
                    page_links = sum(
                        len(_exact_links(str(message.get("text") or message.get("content") or "")))
                        for message in page
                    )
                    page_files = sum(
                        1
                        for message in page
                        for resource in (message.get("resources") or [])
                        if isinstance(resource, dict)
                        and str(resource.get("kind") or "") == "file"
                    )
                    discovered_links += page_links
                    discovered_files += page_files
                    if apply and (page_links or page_files):
                        conn = self._connect()
                        try:
                            conn.execute("BEGIN IMMEDIATE")
                            links, files = self._insert_occurrences(
                                conn, chat, page, self.now_func()
                            )
                            inserted_links += links
                            inserted_files += files
                            conn.commit()
                        except Exception:
                            conn.rollback()
                            raise
                        finally:
                            conn.close()
                    cursor_timestamp, seen_ids = self._cursor_after(
                        page, cursor_timestamp, seen_ids
                    )
                    if len(page) < max_messages:
                        break
        return {
            "state": (
                "source_degraded"
                if degraded_shards
                else "applied" if apply else "planned"
            ),
            "scanned": scanned,
            "discovered_links": discovered_links,
            "discovered_files": discovered_files,
            "inserted_links": inserted_links,
            "inserted_files": inserted_files,
            "degraded_shards": degraded_shards,
            "error_code": source_error_code,
        }

    def _retry_delay(self, attempt_count):
        base = max(1, int(self.config.get("attachment_archive_retry_base_seconds", 300)))
        maximum = max(base, int(self.config.get("attachment_archive_retry_max_seconds", 21600)))
        exponential = min(maximum, base * (2 ** max(0, attempt_count - 1)))
        return max(1, int(exponential * (0.75 + 0.5 * float(self.random_func()))))

    def _set_file_state(self, occurrence_id, status, *, method="", error_code="", values=None):
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT attempt_count FROM resource_occurrences WHERE occurrence_id = ?",
                (occurrence_id,),
            ).fetchone()
            previous = int(row["attempt_count"] or 0) if row else 0
            failure = status in {
                "waiting_cache", "insufficient_local_space", "retry_wait"
            }
            attempt = previous + 1 if failure else 0 if status == "ready_local" else previous
            next_retry = self.now_func() + self._retry_delay(attempt) if failure else 0
            assignments = [
                "status = ?", "resolution_method = ?", "last_error_code = ?",
                "attempt_count = ?", "next_retry_at = ?", "updated_at = ?",
            ]
            params = [status, method, error_code, attempt, next_retry, self.now_func()]
            for key, value in (values or {}).items():
                if key not in {"object_sha256", "object_size", "object_relpath"}:
                    continue
                assignments.append(f"{key} = ?")
                params.append(value)
            params.append(occurrence_id)
            conn.execute(
                "UPDATE resource_occurrences SET " + ", ".join(assignments)
                + " WHERE occurrence_id = ?",
                params,
            )
            conn.commit()
        finally:
            conn.close()

    def resolve_pending_files(self, limit=50):
        chat_keys = self.selected_chat_keys()
        if not chat_keys:
            return {"state": "no_selected_chats", "processed": 0, "ready_local": 0, "failed": 0}
        placeholders = ",".join("?" for _ in chat_keys)
        conn = self._connect()
        try:
            rows = conn.execute(
                f"""
                SELECT * FROM resource_occurrences
                WHERE chat_key IN ({placeholders})
                  AND kind = 'file'
                  AND (
                      status = 'queued'
                      OR (status IN ('waiting_cache', 'insufficient_local_space', 'retry_wait')
                          AND next_retry_at <= ?)
                  )
                ORDER BY CASE WHEN status = 'queued' THEN 0 ELSE 1 END,
                         next_retry_at, occurrence_id
                LIMIT ?
                """,
                (*chat_keys, self.now_func(), max(1, int(limit))),
            ).fetchall()
        finally:
            conn.close()

        processed = 0
        ready_local = 0
        failed = 0
        for row in rows:
            processed += 1
            result = self.archive.preserve_file_mention(dict(row))
            status = str(result.get("status") or "retry_wait")
            method = str(result.get("resolution_method") or "")
            if status == "ready_local":
                self._set_file_state(
                    row["occurrence_id"],
                    "ready_local",
                    method=method,
                    values={
                        "object_sha256": result["sha256"],
                        "object_size": int(result["size"]),
                        "object_relpath": result["object_relpath"],
                    },
                )
                ready_local += 1
            elif status == "missing_retryable":
                self._set_file_state(
                    row["occurrence_id"], "waiting_cache",
                    method=method, error_code=status,
                )
                failed += 1
            elif status in {"ambiguous", "object_too_large", "source_rejected"}:
                self._set_file_state(
                    row["occurrence_id"], status,
                    method=method, error_code=status,
                )
                failed += 1
            elif status == "insufficient_local_space":
                self._set_file_state(
                    row["occurrence_id"], status,
                    method=method, error_code=status,
                )
                failed += 1
            else:
                self._set_file_state(
                    row["occurrence_id"], "retry_wait",
                    method=method,
                    error_code=str(result.get("error_code") or status),
                )
                failed += 1
        return {
            "state": "healthy" if failed == 0 else "degraded",
            "processed": processed,
            "ready_local": ready_local,
            "failed": failed,
        }

    def occurrences(self, *, selected_only=True):
        params = []
        where = ""
        if selected_only:
            chat_keys = self.selected_chat_keys()
            if not chat_keys:
                return []
            where = "WHERE chat_key IN ({})".format(",".join("?" for _ in chat_keys))
            params.extend(chat_keys)
        conn = self._connect()
        try:
            return [
                dict(row)
                for row in conn.execute(
                    f"""
                    SELECT * FROM resource_occurrences
                    {where}
                    ORDER BY chat_alias, source_timestamp, source_message_id,
                             kind, resource_index, occurrence_id
                    """,
                    params,
                )
            ]
        finally:
            conn.close()

    def status(self):
        chats = self.selected_chats()
        keys = [chat["chat_key"] for chat in chats]
        counts = {}
        pending_files = 0
        conn = self._connect()
        try:
            if keys:
                placeholders = ",".join("?" for _ in keys)
                rows = conn.execute(
                    f"""
                    SELECT kind, status, COUNT(*) AS count
                    FROM resource_occurrences
                    WHERE chat_key IN ({placeholders})
                    GROUP BY kind, status
                    ORDER BY kind, status
                    """,
                    keys,
                ).fetchall()
                counts = {
                    f"{row['kind']}:{row['status']}": int(row["count"])
                    for row in rows
                }
                pending_files = int(conn.execute(
                    f"""
                    SELECT COUNT(*) FROM resource_occurrences
                    WHERE chat_key IN ({placeholders})
                      AND kind = 'file' AND status <> 'ready_local'
                    """,
                    keys,
                ).fetchone()[0])
        finally:
            conn.close()
        return {
            "state": self._meta_get("source_state", "not_started"),
            "selected_chats": len(chats),
            "counts": counts,
            "pending_files": pending_files,
            "last_scan_at": self._meta_get("last_scan_at", ""),
            "error_code": self._meta_get("source_error_code", ""),
        }

    def run(self, resolve_limit=50):
        scan = self.scan()
        resolve = self.resolve_pending_files(limit=resolve_limit)
        scan_state = str(scan.get("state") or "")
        resolve_state = str(resolve.get("state") or "")
        if scan_state in {"no_selected_chats", "source_unavailable"}:
            state = scan_state
        elif scan_state == "source_degraded" or resolve_state == "degraded":
            state = "degraded"
        else:
            state = "healthy"
        return {"state": state, "scan": scan, "resolve": resolve}
