"""Mounted-filesystem handoff and Obsidian indexes for captured resources.

The destination may be a Google Drive for Desktop Stream files mount, another
File Provider mount, or an ordinary filesystem directory.  A successful run
proves only that bytes and metadata were durably handed to the mounted target;
it does not claim that a cloud provider has completed remote synchronization.
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import time
import unicodedata
import uuid
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from .attachment_archive import ArchiveError
from .config import DATA_DIR
from .resource_capture import ResourceCaptureError, SelectedResourceCapture


BACKUP_SCHEMA = "we-groupchat-obsidian.resource-backup.v3"
INDEX_MARKER = "<!-- we-groupchat-obsidian:resource-index v1 -->"
PORTAL_MARKER = "<!-- we-groupchat-obsidian:resource-backup-portal v1 -->"
TARGET_PORTAL_NAME = "00-打开微信资源备份.md"
INDEX_MANIFEST_NAME = ".resource-index-manifest.json"
LOCAL_INDEX_MANIFEST_SCHEMA = "we-groupchat-obsidian.resource-index-manifest.v1"
TARGET_INDEX_MANIFEST_SCHEMA = "we-groupchat-obsidian.resource-index-manifest.v2"
PROJECTION_LOCK_DIR = os.path.join(DATA_DIR, "resource-projection-locks")
DESTINATION_MARKER_NAME = ".wgo-destination.json"
DESTINATION_MARKER_SCHEMA = "we-groupchat-obsidian.destination.v1"
SETTINGS_FILE = os.path.join(DATA_DIR, "resource_backup.json")
SETTINGS_LOCK_SUFFIX = ".lock"
OCCURRENCE_ID_DOMAIN = b"we-groupchat-resource-occurrence-v1\0"
SENSITIVE_QUERY_KEYS = {
    "access_token",
    "api_key",
    "auth",
    "authorization",
    "code",
    "credential",
    "credentials",
    "jwt",
    "key",
    "password",
    "secret",
    "session",
    "signature",
    "sig",
    "token",
}


class ResourceBackupError(RuntimeError):
    """Mounted backup failure with a stable, content-free error code."""

    def __init__(self, code):
        super().__init__(str(code))
        self.code = str(code)


def evaluate_resource_backup_outcome(capture_result, backup_result):
    """Return one shared success decision for app and CLI reporting."""
    capture_result = capture_result or {}
    backup_result = backup_result or {}
    capture_state = str(capture_result.get("state") or "unknown")
    scan_state = str(
        (capture_result.get("scan") or {}).get("state") or "unknown"
    )
    resolve_state = str(
        (capture_result.get("resolve") or {}).get("state") or "unknown"
    )
    projection_state = str(
        (backup_result.get("obsidian") or {}).get("state") or "unknown"
    )
    handoff_state = str(backup_result.get("state") or "unknown")
    completed = (
        capture_state == "healthy"
        and scan_state == "healthy"
        and resolve_state in {"healthy", "skipped"}
        and projection_state in {"written", "unchanged"}
        and handoff_state in {"idle", "sync_delegated"}
    )
    if completed:
        state = "ok"
    elif capture_state != "healthy":
        state = capture_state
    elif scan_state != "healthy":
        state = scan_state
    elif resolve_state not in {"healthy", "skipped"}:
        state = resolve_state
    elif projection_state not in {"written", "unchanged"}:
        state = projection_state
    else:
        state = handoff_state
    return {
        "completed": completed,
        "state": state,
        "capture_state": capture_state,
        "scan_state": scan_state,
        "resolve_state": resolve_state,
        "projection_state": projection_state,
        "handoff_state": handoff_state,
    }


def evaluate_link_backfill_outcome(backfill_result, backup_result):
    backfill_result = backfill_result or {}
    source_completed = (
        bool(backfill_result.get("source_complete"))
        and str(backfill_result.get("state") or "") == "applied"
    )
    backup_result = backup_result or {}
    projection_state = str(
        (backup_result.get("obsidian") or {}).get("state") or "unknown"
    )
    handoff_state = str(backup_result.get("state") or "unknown")
    completed = (
        source_completed
        and projection_state in {"written", "unchanged"}
        and handoff_state in {"idle", "sync_delegated"}
    )
    return {
        "completed": completed,
        "state": (
            "ok"
            if completed
            else str(backfill_result.get("state") or "failed")
            if not source_completed
            else projection_state
            if projection_state not in {"written", "unchanged"}
            else handoff_state
        ),
        "projection_state": projection_state,
        "handoff_state": handoff_state,
    }


def _ensure_dir(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _fsync_best_effort(fd):
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno not in {errno.EINVAL, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}:
            raise


def _fsync_dir_best_effort(path):
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        _fsync_best_effort(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_all(fd, data):
    view = memoryview(data)
    written = 0
    while written < len(view):
        count = os.write(fd, view[written:])
        if count <= 0:
            raise OSError("short write")
        written += count


def _hash_fd(fd):
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        digest.update(chunk)
    return size, digest.hexdigest()


def _hash_path(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        mode = os.fstat(fd).st_mode
        if not stat.S_ISREG(mode):
            raise ResourceBackupError("target_not_regular")
        return _hash_fd(fd)
    finally:
        os.close(fd)


def _atomic_bytes(path, data, mode=0o600):
    directory = os.path.dirname(path)
    _ensure_dir(directory)
    fd, temp_path = tempfile.mkstemp(prefix=".partial-", dir=directory)
    try:
        try:
            os.fchmod(fd, mode)
        except OSError:
            pass
        _write_all(fd, data)
        _fsync_best_effort(fd)
        os.close(fd)
        fd = -1
        os.replace(temp_path, path)
        temp_path = ""
        try:
            os.chmod(path, mode)
        except OSError:
            pass
        _fsync_dir_best_effort(directory)
    finally:
        if fd >= 0:
            os.close(fd)
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def _atomic_text_if_changed(path, text):
    data = (str(text).rstrip() + "\n").encode("utf-8")
    try:
        with open(path, "rb") as handle:
            if handle.read() == data:
                return False
    except OSError:
        pass
    _atomic_bytes(path, data)
    return True


def _within(path, root):
    try:
        return os.path.commonpath((os.path.realpath(path), os.path.realpath(root))) == os.path.realpath(root)
    except ValueError:
        return False


def _paths_overlap(left, right):
    try:
        common = os.path.commonpath((left, right))
    except ValueError:
        return False
    return common in {left, right}


def _utf8_prefix(value, max_bytes):
    result = []
    used = 0
    for char in str(value or ""):
        encoded = char.encode("utf-8")
        if used + len(encoded) > max(0, int(max_bytes)):
            break
        result.append(char)
        used += len(encoded)
    return "".join(result)


def _truncate_component(value, fallback, max_bytes, *, preserve_extension=False):
    text = str(value or "") or fallback
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    digest_suffix = "--" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    extension = ""
    stem = text
    if preserve_extension:
        stem, extension = os.path.splitext(text)
        if len(extension.encode("utf-8")) > 32:
            extension = _utf8_prefix(extension, 32)
    suffix = digest_suffix + extension
    stem_budget = max_bytes - len(suffix.encode("utf-8"))
    truncated = _utf8_prefix(stem, stem_budget).rstrip(" ._")
    if not truncated:
        truncated = _utf8_prefix(fallback, stem_budget).rstrip(" ._")
    return (truncated + suffix) if truncated else _utf8_prefix(digest_suffix, max_bytes)


def _safe_part(value, fallback="未命名", max_len=180):
    text = str(value or "").strip()
    chars = []
    for char in text:
        if char in '<>:"/\\|?*' or ord(char) < 32:
            chars.append(" ")
            continue
        category = unicodedata.category(char)
        if category[0] in {"L", "N"} or char in {
            " ", "-", "_", ".", "·", "（", "）", "(", ")", "[", "]", "+",
        }:
            chars.append(char)
        else:
            chars.append(" ")
    cleaned = re.sub(r"\s+", " ", "".join(chars)).strip(" .")
    cleaned = cleaned or fallback
    return _truncate_component(cleaned, fallback, max_len).rstrip(" .")


def _path_collision_key(value):
    return unicodedata.normalize("NFKC", str(value or "")).casefold()


def _safe_subdir(value):
    parts = []
    for part in re.split(r"[/\\]+", str(value or "")):
        clean = _safe_part(part, "", max_len=80)
        if clean and clean not in {".", ".."}:
            parts.append(clean)
    return os.path.join(*parts) if parts else os.path.join("微信群聊", "关注推送")


def _safe_object_name(value):
    name = os.path.basename(str(value or "").strip())
    name = re.sub(r"[\x00-\x1f/:\\]+", "_", name).strip(" ._")
    name = re.sub(r"\s+", " ", name)
    return _truncate_component(
        name or "attachment",
        "attachment",
        189,
        preserve_extension=True,
    )


def _single_line(value, fallback="", limit=240):
    text = "".join(" " if ord(char) < 32 else char for char in str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return (text[:limit] or fallback)


def _markdown_label(value, fallback="链接", *, limit=240):
    text = "".join(" " if ord(char) < 32 else char for char in str(value or ""))
    text = re.sub(r"\s+", " ", text).strip() or fallback
    if limit is not None:
        text = text[:limit]
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\\", "\\\\")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


def _markdown_heading(value, fallback="", *, limit=240):
    return (
        _single_line(value, fallback, limit)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _frontmatter_scalar(value):
    text = str(value or "").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _file_url(path):
    return "file://" + quote(str(path or ""))


def _sensitive_query_key(key):
    lowered = str(key or "").strip().lower()
    if not lowered:
        return False
    if lowered in SENSITIVE_QUERY_KEYS:
        return True
    tokens = [token for token in re.split(r"[^a-z0-9]+", lowered) if token]
    compact = "".join(tokens)
    if any(
        token in {
            "token", "secret", "password", "signature", "sig", "auth",
            "authorization", "credential", "credentials", "jwt", "session",
        }
        for token in tokens
    ):
        return True
    if tokens and tokens[-1] in {"key", "code"}:
        return True
    return any(
        marker in compact
        for marker in (
            "accesstoken",
            "authorization",
            "authtoken",
            "apikey",
            "credential",
            "credentials",
            "jwt",
            "sessionid",
            "sessiontoken",
            "signature",
            "password",
            "secret",
        )
    )


def _redact_url(url):
    try:
        parsed = urlsplit(str(url or ""))
    except ValueError:
        return "REDACTED_INVALID_URL"
    if not parsed.query:
        return str(url or "")
    query = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if _sensitive_query_key(key):
            query.append((key, "REDACTED"))
        else:
            query.append((key, value))
    return urlunsplit(parsed._replace(query=urlencode(query, doseq=True)))


def _markdown_url(url):
    return (
        _single_line(url, "", 8192)
        .replace("<", "%3C")
        .replace(">", "%3E")
    )


def _markdown_relative_url(path):
    return quote(_single_line(path, "", 8192), safe="/-._~")


def _safe_month(value):
    text = str(value or "").strip()
    if re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", text):
        return text
    return "未标月份"


def _stable_occurrence_id(row):
    payload = "\0".join(
        (
            str(row.get("chat_key") or ""),
            str(row.get("source_message_id") or ""),
            str(row.get("kind") or ""),
            str(int(row.get("resource_index") or 0)),
        )
    ).encode("utf-8")
    return "wgo_resource_" + hashlib.sha256(OCCURRENCE_ID_DOMAIN + payload).hexdigest()[:32]


def _canonical_json_bytes(value):
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _canonical_jsonl_bytes(rows):
    return b"".join(_canonical_json_bytes(row) for row in rows)


def _read_resource_backup_settings_unlocked(path):
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError):
        value = {}
    value = value if isinstance(value, dict) else {}
    target = str(value.get("target") or "").strip()
    mode = str(value.get("link_export_mode") or "redacted").strip().lower()
    if mode not in {"full", "redacted", "off"}:
        mode = "redacted"
    return {
        "target": os.path.abspath(os.path.expanduser(target)) if target else "",
        "link_export_mode": mode,
    }


@contextmanager
def _resource_backup_settings_lock(path, *, exclusive):
    lock_path = os.path.abspath(os.path.expanduser(path)) + SETTINGS_LOCK_SUFFIX
    _ensure_dir(os.path.dirname(lock_path))
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def load_resource_backup_settings(path=SETTINGS_FILE):
    with _resource_backup_settings_lock(path, exclusive=False):
        return _read_resource_backup_settings_unlocked(path)


def save_resource_backup_settings(settings, path=SETTINGS_FILE):
    with _resource_backup_settings_lock(path, exclusive=True):
        current = _read_resource_backup_settings_unlocked(path)
        current.update(settings if isinstance(settings, dict) else {})
        target = str(current.get("target") or "").strip()
        mode = str(current.get("link_export_mode") or "redacted").strip().lower()
        if mode not in {"full", "redacted", "off"}:
            raise ValueError("link_export_mode must be full, redacted, or off")
        payload = {
            "target": os.path.abspath(os.path.expanduser(target)) if target else "",
            "link_export_mode": mode,
        }
        _atomic_bytes(path, _canonical_json_bytes(payload))
        return payload


class MountedResourceBackup:
    """Handoff selected resource occurrences to a mounted filesystem target."""

    def __init__(
        self,
        config,
        *,
        capture=None,
        now_func=time.time,
        id_factory=None,
        link_export_mode=None,
        settings_path=SETTINGS_FILE,
    ):
        self.config = dict(config or {})
        self.capture = capture or SelectedResourceCapture.from_config(self.config)
        self.now_func = now_func
        self.id_factory = id_factory or (lambda: os.urandom(4).hex())
        self.settings_path = settings_path
        settings = load_resource_backup_settings(settings_path)
        configured_target = str(
            self.config.get("resource_backup_target") or settings.get("target") or ""
        ).strip()
        self.target = (
            os.path.abspath(os.path.expanduser(configured_target))
            if configured_target else ""
        )
        self.archive_root = self.capture.archive_root
        self.db_path = self.capture.db_path
        self.knowledge_db = os.path.abspath(os.path.expanduser(
            self.config.get("monitor_knowledge_db")
            or os.path.join(DATA_DIR, "monitor_knowledge.db")
        ))
        self.obsidian_root = os.path.abspath(os.path.expanduser(
            self.config.get("monitor_obsidian_root")
            or os.path.join(DATA_DIR, "obsidian_knowledge")
        ))
        self.obsidian_subdir = _safe_subdir(
            self.config.get("monitor_obsidian_subdir") or "微信群聊/关注推送"
        )
        self.projection_lock_dir = os.path.abspath(os.path.expanduser(
            self.config.get("resource_projection_lock_dir")
            or PROJECTION_LOCK_DIR
        ))
        self.min_free_bytes = max(0, int(
            self.config.get(
                "resource_backup_min_free_bytes",
                self.config.get("attachment_archive_min_free_bytes", 1024 * 1024 * 1024),
            )
        ))
        mode = str(
            link_export_mode
            if link_export_mode is not None
            else settings.get("link_export_mode") or "redacted"
        ).strip().lower()
        if mode not in {"full", "redacted", "off"}:
            raise ValueError("link_export_mode must be full, redacted, or off")
        self.link_export_mode = mode
        self._destination_uuid = ""
        self._schema_ready = False

    @classmethod
    def from_config(cls, config, **kwargs):
        return cls(config, **kwargs)

    @property
    def backup_root(self):
        return os.path.join(self.target, "wgo-resource-backup", "v3") if self.target else ""

    @property
    def target_namespace_root(self):
        return os.path.dirname(self.backup_root) if self.backup_root else ""

    @property
    def target_portal_preferred_path(self):
        if not self.target_namespace_root:
            return ""
        return os.path.join(self.target_namespace_root, TARGET_PORTAL_NAME)

    @property
    def _target_portal_candidates(self):
        preferred = self.target_portal_preferred_path
        if not preferred:
            return ()
        stem, extension = os.path.splitext(preferred)
        return (preferred, stem + ".generated" + extension)

    def _portal_marker_owned(self, path):
        if not os.path.lexists(path):
            return False
        try:
            data = self._read_regular_bytes(path)
        except ResourceBackupError:
            return False
        return PORTAL_MARKER.encode("utf-8") in data

    def _managed_target_portal_path(self):
        candidates = self._target_portal_candidates
        for path in candidates:
            if self._portal_marker_owned(path):
                return path
        for path in candidates:
            if not os.path.lexists(path):
                return path
        raise ResourceBackupError("managed_projection_conflict")

    def _write_target_portal(self, text):
        actual = self._managed_target_portal_path()
        changed = self._target_text_if_changed(actual, text)
        for candidate in self._target_portal_candidates:
            if candidate == actual or not self._portal_marker_owned(candidate):
                continue
            try:
                os.unlink(candidate)
            except OSError as exc:
                raise ResourceBackupError("target_file_conflict") from exc
        return actual, changed

    def existing_target_portal_path(self):
        """Return the managed human entrypoint without touching payload bytes."""
        if not self.target_portal_preferred_path or self._target_boundary_error():
            return ""
        for path in self._target_portal_candidates:
            if self._portal_marker_owned(path):
                return path
        return ""

    @property
    def destination_id(self):
        if not self.target:
            return ""
        if self._destination_uuid:
            return self._destination_uuid
        payload = self._read_destination_marker(required=False)
        if payload is None:
            return ""
        return str(payload["destination_uuid"])

    @property
    def _destination_marker_path(self):
        if not self.target:
            return ""
        return os.path.join(
            self.target,
            "wgo-resource-backup",
            DESTINATION_MARKER_NAME,
        )

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self):
        if self._schema_ready:
            return
        conn = self._connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS resource_deliveries (
                    destination_id TEXT NOT NULL,
                    object_sha256 TEXT NOT NULL,
                    object_size INTEGER NOT NULL,
                    target_relpath TEXT NOT NULL,
                    status TEXT NOT NULL,
                    last_error_code TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(destination_id, object_sha256)
                );

                CREATE TABLE IF NOT EXISTS resource_backup_state (
                    destination_id TEXT PRIMARY KEY,
                    catalog_sha256 TEXT NOT NULL DEFAULT '',
                    snapshot_id TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                );
                """
            )
            conn.commit()
        finally:
            conn.close()
        self._schema_ready = True

    @contextmanager
    def _worker_lock(self):
        lock_path = self.db_path + ".resource-backup.lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    raise ResourceBackupError("worker_busy") from exc
                raise
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)

    @property
    def obsidian_projection_root(self):
        return os.path.join(self.obsidian_root, self.obsidian_subdir)

    @contextmanager
    def _projection_worker_lock(self):
        """Serialize aliases of the same local projection root."""
        _ensure_dir(self.projection_lock_dir)
        root_key = os.path.realpath(self.obsidian_projection_root).encode("utf-8")
        lock_path = os.path.join(
            self.projection_lock_dir,
            hashlib.sha256(root_key).hexdigest() + ".lock",
        )
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise ResourceBackupError("projection_lock_unavailable") from exc
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ResourceBackupError("projection_lock_invalid")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    raise ResourceBackupError("worker_busy") from exc
                raise
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)

    @contextmanager
    def _target_worker_lock(self):
        if not self.target or not os.path.isdir(self.target):
            raise ResourceBackupError("destination_unavailable")
        lock_path = os.path.join(self.target, ".wgo-resource-backup.lock")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise ResourceBackupError("target_lock_unavailable") from exc
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ResourceBackupError("target_lock_invalid")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    raise ResourceBackupError("worker_busy") from exc
                raise
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)

    def _read_destination_marker(self, *, required):
        path = self._destination_marker_path
        if not path or not os.path.lexists(path):
            if required:
                raise ResourceBackupError("destination_identity_missing")
            return None
        try:
            data = self._read_regular_bytes(
                path,
                error_code="destination_identity_invalid",
            )
            payload = json.loads(data.decode("utf-8"))
            destination_uuid = str(payload.get("destination_uuid") or "")
            destination_uuid = str(uuid.UUID(destination_uuid))
        except (ResourceBackupError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            if isinstance(exc, ResourceBackupError):
                raise
            raise ResourceBackupError("destination_identity_invalid") from exc
        if payload.get("schema") != DESTINATION_MARKER_SCHEMA:
            raise ResourceBackupError("destination_identity_invalid")
        if str(payload.get("archive_id") or "") != self.capture.archive_id:
            raise ResourceBackupError("destination_archive_mismatch")
        return {**payload, "destination_uuid": destination_uuid}

    def _ensure_destination_identity_owned(self):
        existing = self._read_destination_marker(required=False)
        if existing is not None:
            self._destination_uuid = str(existing["destination_uuid"])
            return self._destination_uuid
        marker_parent = os.path.dirname(self._destination_marker_path)
        self._ensure_target_dir(marker_parent)
        destination_uuid = str(uuid.uuid4())
        payload = _canonical_json_bytes({
            "schema": DESTINATION_MARKER_SCHEMA,
            "archive_id": self.capture.archive_id,
            "destination_uuid": destination_uuid,
        })
        self._target_atomic_bytes(self._destination_marker_path, payload)
        confirmed = self._read_destination_marker(required=True)
        self._destination_uuid = str(confirmed["destination_uuid"])
        return self._destination_uuid

    def _target_boundary_error(self, occurrences=None, object_rows=None):
        if not self.target:
            return ""
        if os.path.lexists(self.target):
            try:
                target_mode = os.lstat(self.target).st_mode
            except OSError:
                return "destination_unavailable"
            if stat.S_ISLNK(target_mode):
                return "target_is_symlink"
        target_forms = {
            os.path.abspath(self.target),
            os.path.realpath(self.target),
        }
        protected = {
            os.path.abspath(self.archive_root),
            os.path.realpath(self.archive_root),
            os.path.abspath(self.db_path),
            os.path.realpath(self.db_path),
            os.path.abspath(self.knowledge_db),
            os.path.realpath(self.knowledge_db),
            os.path.abspath(self.obsidian_root),
            os.path.realpath(self.obsidian_root),
        }
        for target in target_forms:
            if os.path.dirname(target) == target:
                return "target_is_filesystem_root"
            for value in protected:
                if _paths_overlap(target, value):
                    return "target_overlaps_local_source"
        target_real = os.path.realpath(self.target)
        current = os.path.abspath(self.target)
        for part in ("wgo-resource-backup", "v3"):
            current = os.path.join(current, part)
            if not os.path.lexists(current):
                continue
            try:
                mode = os.lstat(current).st_mode
            except OSError:
                return "target_directory_unavailable"
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                return "target_directory_conflict"
            try:
                if os.path.commonpath((os.path.realpath(current), target_real)) != target_real:
                    return "target_directory_escape"
            except ValueError:
                return "target_directory_escape"
        planned = {
            os.path.join(self.backup_root, "objects"),
            os.path.join(self.backup_root, "objects", "sha256"),
            os.path.join(self.backup_root, "snapshots"),
            os.path.join(self.backup_root, "views"),
        }
        for row in object_rows or []:
            planned.add(os.path.dirname(os.path.join(
                self.backup_root, self._target_relpath(row)
            )))
        occurrence_rows = occurrences or []
        chat_parts = self._chat_path_parts(occurrence_rows)
        chats_with_files = {
            (
                str(row.get("chat_key") or ""),
                str(row.get("chat_alias") or "未命名群聊"),
            )
            for row in occurrence_rows
            if row.get("kind") == "file"
        }
        for identity, chat_part in chat_parts.items():
            chat_root = os.path.join(self.backup_root, "views", chat_part)
            planned.add(chat_root)
            planned.add(os.path.join(chat_root, "资源索引"))
            if identity in chats_with_files:
                planned.add(os.path.join(chat_root, "文件备份"))
        for path in sorted(planned):
            relative = os.path.relpath(path, self.backup_root)
            current = self.backup_root
            for part in relative.split(os.sep):
                if part in {"", "."}:
                    continue
                current = os.path.join(current, part)
                if not os.path.lexists(current):
                    continue
                try:
                    mode = os.lstat(current).st_mode
                except OSError:
                    return "target_directory_unavailable"
                if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                    return "target_subtree_conflict"
                try:
                    if os.path.commonpath(
                        (os.path.realpath(current), target_real)
                    ) != target_real:
                        return "target_directory_escape"
                except ValueError:
                    return "target_directory_escape"
        return ""

    def _ensure_target_dir(self, path):
        """Create an app-owned target directory without following child symlinks."""
        if not self.target:
            raise ResourceBackupError("target_not_configured")
        target = os.path.abspath(self.target)
        target_real = os.path.realpath(target)
        path = os.path.abspath(path)
        try:
            if os.path.commonpath((path, target)) != target:
                raise ResourceBackupError("target_outside_configured_root")
        except ValueError as exc:
            raise ResourceBackupError("target_outside_configured_root") from exc
        try:
            target_mode = os.lstat(target).st_mode
        except OSError as exc:
            raise ResourceBackupError("destination_unavailable") from exc
        if stat.S_ISLNK(target_mode):
            raise ResourceBackupError("target_is_symlink")
        if not stat.S_ISDIR(target_mode):
            raise ResourceBackupError("destination_unavailable")

        relative = os.path.relpath(path, target)
        if relative == ".":
            return
        current = target
        for part in relative.split(os.sep):
            if part in {"", ".", ".."}:
                raise ResourceBackupError("target_path_invalid")
            current = os.path.join(current, part)
            if os.path.lexists(current):
                try:
                    mode = os.lstat(current).st_mode
                except OSError as exc:
                    raise ResourceBackupError("target_directory_unavailable") from exc
                if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                    raise ResourceBackupError("target_directory_conflict")
            else:
                try:
                    os.mkdir(current, 0o700)
                except OSError as exc:
                    raise ResourceBackupError("target_directory_unavailable") from exc
            try:
                if os.path.commonpath((os.path.realpath(current), target_real)) != target_real:
                    raise ResourceBackupError("target_directory_escape")
            except ValueError as exc:
                raise ResourceBackupError("target_directory_escape") from exc
            try:
                os.chmod(current, 0o700)
            except OSError:
                pass

    def _ensure_projection_dir(self, path):
        """Create a local projection directory without following child symlinks."""
        anchor = os.path.abspath(self.obsidian_root)
        path = os.path.abspath(path)
        try:
            if os.path.commonpath((path, anchor)) != anchor:
                raise ResourceBackupError("projection_outside_configured_root")
        except ValueError as exc:
            raise ResourceBackupError("projection_outside_configured_root") from exc
        _ensure_dir(anchor)
        if not os.path.isdir(anchor):
            raise ResourceBackupError("projection_root_unavailable")
        anchor_real = os.path.realpath(anchor)
        relative = os.path.relpath(path, anchor)
        if relative == ".":
            return
        current = anchor
        for part in relative.split(os.sep):
            if part in {"", ".", ".."}:
                raise ResourceBackupError("projection_path_invalid")
            current = os.path.join(current, part)
            if os.path.lexists(current):
                try:
                    mode = os.lstat(current).st_mode
                except OSError as exc:
                    raise ResourceBackupError(
                        "projection_directory_unavailable"
                    ) from exc
                if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                    raise ResourceBackupError("projection_directory_conflict")
            else:
                try:
                    os.mkdir(current, 0o700)
                except OSError as exc:
                    raise ResourceBackupError(
                        "projection_directory_unavailable"
                    ) from exc
            try:
                if os.path.commonpath(
                    (os.path.realpath(current), anchor_real)
                ) != anchor_real:
                    raise ResourceBackupError("projection_directory_escape")
            except ValueError as exc:
                raise ResourceBackupError("projection_directory_escape") from exc
            try:
                os.chmod(current, 0o700)
            except OSError:
                pass

    def _projection_atomic_bytes(self, path, data, mode=0o600):
        self._ensure_projection_dir(os.path.dirname(path))
        if os.path.lexists(path):
            try:
                existing_mode = os.lstat(path).st_mode
            except OSError as exc:
                raise ResourceBackupError("projection_file_conflict") from exc
            if stat.S_ISLNK(existing_mode) or not stat.S_ISREG(existing_mode):
                raise ResourceBackupError("projection_file_conflict")
        _atomic_bytes(path, data, mode=mode)
        if not _within(path, self.obsidian_root):
            raise ResourceBackupError("projection_file_escape")

    def _projection_text_if_changed(self, path, text):
        data = (str(text).rstrip() + "\n").encode("utf-8")
        self._ensure_projection_dir(os.path.dirname(path))
        if os.path.lexists(path):
            current = self._read_regular_bytes(
                path,
                error_code="projection_file_conflict",
            )
            if current == data:
                return False
        self._projection_atomic_bytes(path, data)
        return True

    @staticmethod
    def _read_regular_bytes(path, *, error_code="target_file_conflict"):
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise ResourceBackupError(error_code) from exc
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ResourceBackupError(error_code)
            chunks = []
            while True:
                chunk = os.read(fd, 64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)

    def _target_atomic_bytes(self, path, data, mode=0o600):
        directory = os.path.dirname(path)
        self._ensure_target_dir(directory)
        if os.path.lexists(path):
            try:
                existing_mode = os.lstat(path).st_mode
            except OSError as exc:
                raise ResourceBackupError("target_file_conflict") from exc
            if stat.S_ISLNK(existing_mode) or not stat.S_ISREG(existing_mode):
                raise ResourceBackupError("target_file_conflict")
        _atomic_bytes(path, data, mode=mode)
        if not _within(path, self.target):
            raise ResourceBackupError("target_file_escape")

    def _target_text_if_changed(self, path, text):
        data = (str(text).rstrip() + "\n").encode("utf-8")
        if os.path.lexists(path):
            current = self._read_regular_bytes(path)
            if current == data:
                return False
        self._target_atomic_bytes(path, data)
        return True

    def _managed_text_path(self, preferred_path, *, target_view=False):
        stem, extension = os.path.splitext(preferred_path)
        candidates = (preferred_path, stem + ".generated" + (extension or ".md"))
        for candidate in candidates:
            if not target_view:
                self._ensure_projection_dir(os.path.dirname(candidate))
            if not os.path.lexists(candidate):
                return candidate
            try:
                data = (
                    self._read_regular_bytes(candidate)
                    if target_view
                    else MountedResourceBackup._read_regular_bytes(
                        candidate, error_code="projection_file_conflict"
                    )
                )
            except ResourceBackupError:
                continue
            if INDEX_MARKER.encode("utf-8") in data:
                return candidate
        raise ResourceBackupError("managed_projection_conflict")

    def _write_managed_text(self, preferred_path, text, *, target_view=False):
        actual_path = self._managed_text_path(
            preferred_path, target_view=target_view
        )
        if target_view:
            changed = self._target_text_if_changed(actual_path, text)
        else:
            changed = self._projection_text_if_changed(actual_path, text)
        return actual_path, changed

    def _managed_projection_paths(self, base_root, *, target_view):
        if not target_view:
            self._ensure_projection_dir(base_root)
        manifest_path = os.path.join(base_root, INDEX_MANIFEST_NAME)
        if not os.path.lexists(manifest_path):
            legacy_paths = set()
            if os.path.isdir(base_root):
                for root, dirs, files in os.walk(base_root, followlinks=False):
                    dirs[:] = [
                        name for name in dirs
                        if not os.path.islink(os.path.join(root, name))
                    ]
                    for name in files:
                        path = os.path.join(root, name)
                        try:
                            data = (
                                self._read_regular_bytes(path)
                                if target_view
                                else MountedResourceBackup._read_regular_bytes(
                                    path,
                                    error_code="projection_file_conflict",
                                )
                            )
                        except ResourceBackupError:
                            continue
                        if INDEX_MARKER.encode("utf-8") in data:
                            legacy_paths.add(
                                os.path.relpath(path, base_root).replace(os.sep, "/")
                            )
            # Pre-manifest releases still marked every generated index with the
            # exact app ownership marker. Adopt only those files for one
            # reconciliation pass; unmarked user files never enter managed GC.
            # A successful render immediately writes the normal manifest.
            return legacy_paths
        try:
            data = (
                self._read_regular_bytes(manifest_path)
                if target_view
                else MountedResourceBackup._read_regular_bytes(
                    manifest_path, error_code="projection_manifest_invalid"
                )
            )
            payload = json.loads(data.decode("utf-8"))
            expected_schema = (
                TARGET_INDEX_MANIFEST_SCHEMA
                if target_view else LOCAL_INDEX_MANIFEST_SCHEMA
            )
            accepted_schemas = {expected_schema}
            if target_view:
                accepted_schemas.add(LOCAL_INDEX_MANIFEST_SCHEMA)
            if (
                not isinstance(payload, dict)
                or payload.get("schema") not in accepted_schemas
                or not isinstance(payload.get("paths"), list)
            ):
                raise ValueError("invalid manifest")
            if str(payload.get("archive_id") or "") != self.capture.archive_id:
                raise ResourceBackupError("projection_archive_mismatch")
            if target_view and str(payload.get("destination_id") or "") != self.destination_id:
                raise ResourceBackupError("projection_destination_mismatch")
            result = set()
            for value in payload["paths"]:
                relative = str(value or "")
                candidate = os.path.abspath(os.path.join(base_root, relative))
                if (
                    relative
                    and not os.path.isabs(relative)
                    and _within(candidate, base_root)
                ):
                    result.add(relative.replace("\\", "/"))
            return result
        except ResourceBackupError:
            raise
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise ResourceBackupError("projection_manifest_invalid") from exc

    def _reconcile_managed_projection(
        self,
        base_root,
        current_paths,
        *,
        target_view,
        previous_paths=None,
    ):
        current = {
            os.path.relpath(path, base_root).replace(os.sep, "/")
            for path in current_paths
        }
        previous = (
            self._managed_projection_paths(base_root, target_view=target_view)
            if previous_paths is None
            else set(previous_paths)
        )
        for relative in sorted(previous - current, reverse=True):
            path = os.path.abspath(os.path.join(base_root, relative))
            if not _within(path, base_root):
                continue
            if not target_view:
                self._ensure_projection_dir(os.path.dirname(path))
            try:
                data = (
                    self._read_regular_bytes(path)
                    if target_view
                    else MountedResourceBackup._read_regular_bytes(
                        path, error_code="projection_file_conflict"
                    )
                )
            except ResourceBackupError:
                continue
            if INDEX_MARKER.encode("utf-8") not in data:
                continue
            try:
                os.unlink(path)
            except OSError:
                continue
            parent = os.path.dirname(path)
            while parent != base_root and _within(parent, base_root):
                try:
                    os.rmdir(parent)
                except OSError:
                    break
                parent = os.path.dirname(parent)
        payload = _canonical_json_bytes({
            "schema": (
                TARGET_INDEX_MANIFEST_SCHEMA
                if target_view else LOCAL_INDEX_MANIFEST_SCHEMA
            ),
            "archive_id": self.capture.archive_id,
            "destination_id": self.destination_id if target_view else "local",
            "paths": sorted(current),
        })
        manifest_path = os.path.join(base_root, INDEX_MANIFEST_NAME)
        if target_view:
            self._target_atomic_bytes(manifest_path, payload)
        else:
            self._projection_atomic_bytes(manifest_path, payload)

    def _delivery_row(self, digest):
        conn = self._connect()
        try:
            return conn.execute(
                """
                SELECT * FROM resource_deliveries
                WHERE destination_id = ? AND object_sha256 = ?
                """,
                (self.destination_id, digest),
            ).fetchone()
        finally:
            conn.close()

    def _record_delivery(self, digest, size, relpath, status, error_code=""):
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO resource_deliveries(
                    destination_id, object_sha256, object_size, target_relpath,
                    status, last_error_code, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(destination_id, object_sha256) DO UPDATE SET
                    object_size = excluded.object_size,
                    target_relpath = excluded.target_relpath,
                    status = excluded.status,
                    last_error_code = excluded.last_error_code,
                    updated_at = excluded.updated_at
                """,
                (
                    self.destination_id, digest, int(size), relpath,
                    status, error_code, self.now_func(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _state_row(self):
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT * FROM resource_backup_state WHERE destination_id = ?",
                (self.destination_id,),
            ).fetchone()
        finally:
            conn.close()

    def _record_snapshot_state(self, catalog_sha256, snapshot_id):
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO resource_backup_state(
                    destination_id, catalog_sha256, snapshot_id, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(destination_id) DO UPDATE SET
                    catalog_sha256 = excluded.catalog_sha256,
                    snapshot_id = excluded.snapshot_id,
                    updated_at = excluded.updated_at
                """,
                (self.destination_id, catalog_sha256, snapshot_id, self.now_func()),
            )
            conn.commit()
        finally:
            conn.close()

    def _source_path(self, occurrence):
        path = os.path.realpath(os.path.join(
            self.archive_root, str(occurrence.get("object_relpath") or "")
        ))
        if not _within(path, self.archive_root):
            raise ResourceBackupError("source_outside_archive")
        try:
            mode = os.lstat(path).st_mode
        except OSError as exc:
            raise ResourceBackupError("source_missing") from exc
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise ResourceBackupError("source_not_regular")
        return path

    def _target_relpath(self, occurrence):
        digest = str(occurrence["object_sha256"])
        filename = f"{digest}--{_safe_object_name(occurrence.get('original_name'))}"
        return os.path.join("objects", "sha256", digest[:2], filename)

    def _copy_object(self, occurrence):
        digest = str(occurrence.get("object_sha256") or "")
        size = int(occurrence.get("object_size") or 0)
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ResourceBackupError("object_identity_invalid")
        receipt = self._delivery_row(digest)
        relpath = str(receipt["target_relpath"]) if receipt else self._target_relpath(occurrence)
        target_path = os.path.join(self.backup_root, relpath)
        if not _within(target_path, self.backup_root):
            raise ResourceBackupError("target_outside_backup_root")
        directory = os.path.dirname(target_path)
        self._ensure_target_dir(directory)
        if (
            receipt is not None
            and receipt["status"] == "sync_delegated"
            and int(receipt["object_size"]) == size
        ):
            if os.path.lexists(target_path):
                try:
                    target_stat = os.lstat(target_path)
                except OSError as exc:
                    raise ResourceBackupError("target_read_failed") from exc
                if stat.S_ISLNK(target_stat.st_mode) or not stat.S_ISREG(target_stat.st_mode):
                    raise ResourceBackupError("target_object_conflict")
                if int(target_stat.st_size) != size:
                    raise ResourceBackupError("target_object_conflict")
                return "already_delegated", relpath

        source_path = self._source_path(occurrence)

        if os.path.lexists(target_path):
            try:
                mode = os.lstat(target_path).st_mode
                if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                    raise ResourceBackupError("target_object_conflict")
                target_size, target_digest = _hash_path(target_path)
            except OSError as exc:
                raise ResourceBackupError("target_read_failed") from exc
            if target_size != size or target_digest != digest:
                raise ResourceBackupError("target_object_conflict")
            self._record_delivery(digest, size, relpath, "sync_delegated")
            return "target_verified_once", relpath

        try:
            free_bytes = int(shutil.disk_usage(self.target).free)
        except OSError as exc:
            raise ResourceBackupError("target_space_unavailable") from exc
        if free_bytes - size < self.min_free_bytes:
            raise ResourceBackupError("insufficient_target_space")

        try:
            for entry in os.scandir(directory):
                if entry.name.startswith(".partial-") and entry.is_file(follow_symlinks=False):
                    try:
                        os.unlink(entry.path)
                    except OSError:
                        pass
        except OSError:
            pass
        source_fd = os.open(
            source_path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        target_fd = -1
        temp_path = ""
        try:
            before = os.fstat(source_fd)
            if not stat.S_ISREG(before.st_mode):
                raise ResourceBackupError("source_not_regular")
            target_fd, temp_path = tempfile.mkstemp(prefix=".partial-", dir=directory)
            try:
                os.fchmod(target_fd, 0o600)
            except OSError:
                pass
            copied_digest = hashlib.sha256()
            copied_size = 0
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                _write_all(target_fd, chunk)
                copied_size += len(chunk)
                copied_digest.update(chunk)
            _fsync_best_effort(target_fd)
            after = os.fstat(source_fd)
            if any(
                getattr(before, field) != getattr(after, field)
                for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns")
            ):
                raise ResourceBackupError("source_changed")
            if copied_size != size or copied_digest.hexdigest() != digest:
                raise ResourceBackupError("source_hash_mismatch")
            os.close(target_fd)
            target_fd = -1
            os.replace(temp_path, target_path)
            temp_path = ""
            try:
                os.chmod(target_path, 0o600)
            except OSError:
                pass
            _fsync_dir_best_effort(directory)

            # One immediate readback proves the mounted destination received the
            # intended bytes.  Later scheduled runs trust the local receipt and do
            # not hydrate/re-read a streamed cloud placeholder.
            target_size, target_digest = _hash_path(target_path)
            if target_size != size or target_digest != digest:
                raise ResourceBackupError("target_readback_mismatch")
            self._record_delivery(digest, size, relpath, "sync_delegated")
            return "copied", relpath
        finally:
            os.close(source_fd)
            if target_fd >= 0:
                os.close(target_fd)
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def _delivery_map(self):
        if not self.destination_id:
            return {}
        conn = self._connect()
        try:
            return {
                str(row["object_sha256"]): dict(row)
                for row in conn.execute(
                    "SELECT * FROM resource_deliveries WHERE destination_id = ?",
                    (self.destination_id,),
                )
            }
        finally:
            conn.close()

    def _export_url(self, url):
        if self.link_export_mode == "off":
            return ""
        if self.link_export_mode == "redacted":
            return _redact_url(url)
        return str(url or "")

    def _catalog_records(self, occurrences, delivery_map):
        records = []
        for row in occurrences:
            kind = str(row.get("kind") or "")
            digest = str(row.get("object_sha256") or "")
            delivery = delivery_map.get(digest) if digest else None
            record = {
                "occurrence_id": _stable_occurrence_id(row),
                "kind": kind,
                "chat_key": str(row.get("chat_key") or ""),
                "chat_alias": str(row.get("chat_alias") or ""),
                "source_message_id": str(row.get("source_message_id") or ""),
                "resource_index": int(row.get("resource_index") or 0),
                "source_timestamp": int(row.get("source_timestamp") or 0),
                "source_time": str(row.get("source_time") or ""),
                "source_sender": str(row.get("source_sender") or ""),
                "source_month": str(row.get("source_month") or ""),
                "original_name": str(row.get("original_name") or ""),
                "observed_url": self._export_url(row.get("observed_url") or "") if kind == "link" else "",
                "url_sha256": str(row.get("url_sha256") or ""),
                "object_sha256": digest,
                "object_size": (
                    int(row["object_size"]) if row.get("object_size") is not None else None
                ),
                "capture_status": str(row.get("status") or ""),
                "handoff_status": str(delivery.get("status") or "") if delivery else "",
                "target_relpath": str(delivery.get("target_relpath") or "") if delivery else "",
            }
            records.append(record)
        return records

    def _object_rows(self, occurrences):
        rows = {}
        for occurrence in occurrences:
            if (
                occurrence.get("kind") != "file"
                or occurrence.get("status") != "ready_local"
                or not occurrence.get("object_sha256")
            ):
                continue
            digest = str(occurrence["object_sha256"])
            existing = rows.get(digest)
            if existing and int(existing["object_size"]) != int(occurrence["object_size"]):
                raise ResourceBackupError("object_identity_conflict")
            rows.setdefault(digest, occurrence)
        return [rows[key] for key in sorted(rows)]

    def _snapshot_id(self):
        stamp = datetime.fromtimestamp(self.now_func(), tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"{stamp}-{self.id_factory()}"

    def _write_snapshot(self, records, object_rows, delivery_map):
        resources_bytes = _canonical_jsonl_bytes(records)
        resources_sha256 = hashlib.sha256(resources_bytes).hexdigest()
        unresolved_files = sum(
            1 for row in records
            if row["kind"] == "file" and row["capture_status"] != "ready_local"
        )
        handoff_semantics = (
            "pending_resources" if unresolved_files else "sync_delegated"
        )
        state = self._state_row()
        if state and state["catalog_sha256"] == resources_sha256:
            existing = self._load_snapshot(str(state["snapshot_id"]))
            manifest = existing["manifest"] if existing else {}
            if (
                manifest.get("resources_sha256") == resources_sha256
                and manifest.get("archive_id") == self.capture.archive_id
                and manifest.get("link_export_mode") == self.link_export_mode
                and manifest.get("handoff_semantics") == handoff_semantics
            ):
                return {
                    "state": "unchanged",
                    "snapshot_id": str(state["snapshot_id"]),
                    "catalog_sha256": resources_sha256,
                    "handoff_semantics": handoff_semantics,
                    "unresolved_files": unresolved_files,
                }

        snapshot_id = self._snapshot_id()
        snapshot_dir = os.path.join(self.backup_root, "snapshots", snapshot_id)
        self._ensure_target_dir(snapshot_dir)
        resources_path = os.path.join(snapshot_dir, "resources.jsonl")
        self._target_atomic_bytes(resources_path, resources_bytes)
        manifest = {
            "schema": BACKUP_SCHEMA,
            "archive_id": self.capture.archive_id,
            "snapshot_id": snapshot_id,
            "created_at": datetime.fromtimestamp(
                self.now_func(), tz=timezone.utc
            ).isoformat(),
            "snapshot_completeness": "catalog_complete",
            "handoff_semantics": handoff_semantics,
            "remote_verification": False,
            "link_export_mode": self.link_export_mode,
            "resource_count": len(records),
            "link_count": sum(1 for row in records if row["kind"] == "link"),
            "file_occurrence_count": sum(1 for row in records if row["kind"] == "file"),
            "object_count": len(object_rows),
            "unresolved_file_count": unresolved_files,
            "objects": [
                {
                    "sha256": str(row["object_sha256"]),
                    "size": int(row["object_size"]),
                    "target_relpath": str(
                        (delivery_map.get(str(row["object_sha256"])) or {}).get(
                            "target_relpath", ""
                        )
                    ),
                }
                for row in object_rows
            ],
            "resources_file": "resources.jsonl",
            "resources_sha256": resources_sha256,
        }
        manifest_bytes = _canonical_json_bytes(manifest)
        self._target_atomic_bytes(
            os.path.join(snapshot_dir, "manifest.json"), manifest_bytes
        )
        complete = {
            "schema": BACKUP_SCHEMA,
            "snapshot_id": snapshot_id,
            "state": "complete",
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "resources_sha256": resources_sha256,
        }
        self._target_atomic_bytes(
            os.path.join(snapshot_dir, "COMPLETE"), _canonical_json_bytes(complete)
        )
        self._record_snapshot_state(resources_sha256, snapshot_id)
        return {
            "state": "written",
            "snapshot_id": snapshot_id,
            "catalog_sha256": resources_sha256,
            "handoff_semantics": handoff_semantics,
            "unresolved_files": unresolved_files,
        }

    def _render_month(
        self, chat_alias, month, rows, delivery_map, *, target_view=False
    ):
        display_chat = _single_line(chat_alias, "未命名群聊", 120)
        display_month = _single_line(month, "未标月份", 20)
        lines = [
            "---",
            "source_app: we-groupchat-obsidian",
            "source_kind: resource_index",
            "source_schema_version: 2",
            f"source_chat: {_frontmatter_scalar(display_chat)}",
            f"month: {_frontmatter_scalar(display_month)}",
            "---",
            "",
            INDEX_MARKER,
            f"# {_markdown_heading(display_chat)} · {_markdown_heading(display_month)} 资源索引",
            "",
            "> 点击即可打开；详细来源与归档记录保留在本地 catalog。",
            "",
        ]
        current_day = ""
        for item in rows:
            when = str(item.get("source_time") or "")
            if not when and item.get("source_timestamp"):
                when = datetime.fromtimestamp(
                    int(item["source_timestamp"])
                ).strftime("%Y-%m-%d %H:%M")
            day = _single_line(
                when[5:10] if len(when) >= 10 else display_month,
                display_month,
                20,
            )
            clock = _single_line(
                when[11:16] if len(when) >= 16 else "",
                "--:--",
                10,
            )
            if day != current_day:
                if current_day:
                    lines.append("")
                lines.extend([f"## {day}", ""])
                current_day = day

            if item.get("kind") == "link":
                url = (
                    self._export_url(item.get("observed_url") or "")
                    if target_view else str(item.get("observed_url") or "")
                )
                title = str(item.get("original_name") or "").strip()
                if url:
                    label_source = title or url
                    label = _markdown_label(
                        label_source,
                        "链接",
                        limit=240 if title else None,
                    )
                    lines.append(
                        f"- {clock} · 🔗 [{label}](<{_markdown_url(url)}>)"
                    )
                else:
                    label = _markdown_label(title, "链接")
                    lines.append(f"- {clock} · 🔗 {label}（URL 未导出）")
                continue

            name = _markdown_label(item.get("original_name"), "attachment")
            digest = str(item.get("object_sha256") or "")
            delivery = delivery_map.get(digest) if digest else None
            link = ""
            unavailable = ""
            if target_view:
                relpath = str(delivery.get("target_relpath") or "") if delivery else ""
                if relpath:
                    link = os.path.join("..", "..", "..", relpath).replace(
                        os.sep,
                        "/",
                    )
                else:
                    unavailable = "（待同步）"
            elif digest and item.get("object_relpath"):
                local_path = os.path.join(
                    self.archive_root,
                    str(item["object_relpath"]),
                )
                link = _file_url(local_path)
            else:
                unavailable = "（等待本地文件）"
            if link:
                lines.append(
                    f"- {clock} · 📎 [{name}](<{_markdown_relative_url(link)}>)"
                )
            else:
                lines.append(f"- {clock} · 📎 {name}{unavailable}")
        return "\n".join(lines).rstrip() + "\n"

    def _ready_delivery(self, item, delivery_map):
        if item.get("kind") != "file" or item.get("status") != "ready_local":
            return None
        digest = str(item.get("object_sha256") or "")
        delivery = delivery_map.get(digest) if digest else None
        relpath = str(delivery.get("target_relpath") or "") if delivery else ""
        target_path = os.path.join(self.backup_root, relpath) if relpath else ""
        if (
            not delivery
            or str(delivery.get("status") or "") != "sync_delegated"
            or int(delivery.get("object_size") or -1)
            != int(item.get("object_size") or -2)
            or not relpath
            or os.path.isabs(relpath)
            or not _within(target_path, self.backup_root)
        ):
            return None
        return delivery

    def _file_delivery_counts(self, rows, delivery_map):
        ready = 0
        pending = 0
        for item in rows:
            if item.get("kind") != "file":
                continue
            if self._ready_delivery(item, delivery_map):
                ready += 1
            else:
                pending += 1
        return ready, pending

    def _render_file_month(self, chat_alias, month, rows, delivery_map):
        display_chat = _single_line(chat_alias, "未命名群聊", 120)
        display_month = _single_line(month, "未标月份", 20)
        ready = []
        pending = []
        for item in rows:
            if item.get("kind") != "file":
                continue
            delivery = self._ready_delivery(item, delivery_map)
            if delivery:
                ready.append((item, delivery))
            else:
                pending.append(item)
        lines = [
            "---",
            "source_app: we-groupchat-obsidian",
            "source_kind: file_backup_index",
            "source_schema_version: 1",
            f"source_chat: {_frontmatter_scalar(display_chat)}",
            f"month: {_frontmatter_scalar(display_month)}",
            "---",
            "",
            INDEX_MARKER,
            f"# {_markdown_heading(display_chat)} · {_markdown_heading(display_month)} 文件备份",
            "",
            "> 这里仅列文件；“已备份”只表示 bytes 已写入并即时校验到当前挂载目录，不表示云端同步完成。",
            "> 同一份 bytes 只保存在系统 CAS 中，点击文件名即可打开。",
            "",
            f"## 已备份，可点击打开（{len(ready)}）",
            "",
        ]
        if ready:
            for item, delivery in ready:
                name = _markdown_label(item.get("original_name"), "attachment")
                when = _single_line(
                    item.get("source_time"),
                    display_month,
                    32,
                )
                link = os.path.join(
                    "..",
                    "..",
                    "..",
                    str(delivery["target_relpath"]),
                ).replace(os.sep, "/")
                lines.append(
                    f"- {when} · 📎 [{name}](<{_markdown_relative_url(link)}>)"
                )
        else:
            lines.append("本月还没有已备份文件。")
        lines.extend(["", f"## 尚未备份（{len(pending)}）", ""])
        if pending:
            for item in pending:
                name = _markdown_label(item.get("original_name"), "attachment")
                when = _single_line(
                    item.get("source_time"),
                    display_month,
                    32,
                )
                lines.append(f"- {when} · 📎 {name}（等待本地附件解析）")
        else:
            lines.append("没有待解析文件。")
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _chat_path_parts(occurrences):
        aliases = {}
        collision_groups = defaultdict(set)
        for row in occurrences:
            chat_key = str(row.get("chat_key") or "")
            chat_alias = str(row.get("chat_alias") or "未命名群聊")
            safe = _safe_part(chat_alias, "未命名群聊")
            aliases[(chat_key, chat_alias)] = safe
            collision_groups[_path_collision_key(safe)].add(chat_key)
        reserved = {
            _path_collision_key(name)
            for name in (
                "00-文件备份.md",
                "00-文件备份.generated.md",
                "00-资源索引.md",
                "00-资源索引.generated.md",
            )
        }
        return {
            identity: (
                f"{safe}--{identity[0][:8]}"
                if (
                    len(collision_groups[_path_collision_key(safe)]) > 1
                    or _path_collision_key(safe) in reserved
                )
                else safe
            )
            for identity, safe in aliases.items()
        }

    def _render_indexes_at(self, base_root, occurrences, *, target_view=False):
        # Validate projection ownership before touching any generated path.
        previous_managed_paths = self._managed_projection_paths(
            base_root,
            target_view=target_view,
        )
        grouped = defaultdict(lambda: defaultdict(list))
        for row in occurrences:
            chat_key = str(row.get("chat_key") or "")
            chat_alias = str(row.get("chat_alias") or "未命名群聊")
            grouped[(chat_key, chat_alias)][
                _safe_month(row.get("source_month"))
            ].append(row)
        chat_parts = self._chat_path_parts(occurrences)
        delivery_map = self._delivery_map() if target_view else {}
        written = 0
        managed_paths = []
        chat_summaries = []
        for (chat_key, chat_alias), months in grouped.items():
            chat_part = chat_parts[(chat_key, chat_alias)]
            chat_root = os.path.join(base_root, chat_part)
            index_dir = os.path.join(chat_root, "资源索引")
            file_index_dir = os.path.join(chat_root, "文件备份")
            chat_rows = [row for rows in months.values() for row in rows]
            chat_has_files = any(
                row.get("kind") == "file" for row in chat_rows
            )
            if target_view:
                self._ensure_target_dir(index_dir)
                if chat_has_files:
                    self._ensure_target_dir(file_index_dir)
            else:
                self._ensure_projection_dir(index_dir)
            index_lines = [
                INDEX_MARKER,
                f"# {_markdown_heading(chat_alias, '未命名群聊', limit=120)} · 资源索引",
                "",
                "> 按月份查看链接与文件。",
                "",
            ]
            file_month_summaries = []
            for month in sorted(months, reverse=True):
                rows = sorted(
                    months[month],
                    key=lambda item: (
                        int(item.get("source_timestamp") or 0),
                        str(item.get("source_message_id") or ""),
                        str(item.get("kind") or ""),
                        int(item.get("resource_index") or 0),
                    ),
                )
                links = sum(1 for row in rows if row.get("kind") == "link")
                files = sum(1 for row in rows if row.get("kind") == "file")
                month_preferred_path = os.path.join(index_dir, month + ".md")
                month_path = self._managed_text_path(
                    month_preferred_path,
                    target_view=target_view,
                )
                month_stem = os.path.splitext(os.path.basename(month_path))[0]
                if target_view:
                    index_lines.append(
                        f"- [{month}](资源索引/{month_stem}.md) · {links} 个链接 · {files} 个文件"
                    )
                else:
                    index_lines.append(
                        f"- [[资源索引/{month_stem}|{month}]] · {links} 个链接 · {files} 个文件"
                    )
                month_text = self._render_month(
                    chat_alias,
                    month,
                    rows,
                    delivery_map,
                    target_view=target_view,
                )
                _actual_month_path, month_changed = self._write_managed_text(
                    month_preferred_path,
                    month_text,
                    target_view=target_view,
                )
                if month_changed:
                    written += 1
                managed_paths.append(_actual_month_path)

                file_rows = [row for row in rows if row.get("kind") == "file"]
                if target_view and file_rows:
                    ready, pending = self._file_delivery_counts(
                        file_rows,
                        delivery_map,
                    )
                    file_month_preferred = os.path.join(
                        file_index_dir,
                        month + ".md",
                    )
                    actual_file_month, file_month_changed = (
                        self._write_managed_text(
                            file_month_preferred,
                            self._render_file_month(
                                chat_alias,
                                month,
                                file_rows,
                                delivery_map,
                            ),
                            target_view=True,
                        )
                    )
                    if file_month_changed:
                        written += 1
                    managed_paths.append(actual_file_month)
                    file_month_summaries.append({
                        "month": month,
                        "name": os.path.basename(actual_file_month),
                        "ready": ready,
                        "pending": pending,
                    })

            ready_files = pending_files = 0
            actual_file_chat = ""
            if target_view and chat_has_files:
                ready_files, pending_files = self._file_delivery_counts(
                    chat_rows,
                    delivery_map,
                )
                file_index_lines = [
                    INDEX_MARKER,
                    f"# {_markdown_heading(chat_alias, '未命名群聊', limit=120)} · 文件备份",
                    "",
                    "> 只看文件，不混入网页链接。点击月份后可直接打开已备份文件。",
                    "",
                    f"**{ready_files} 条可打开 · {pending_files} 条待解析**",
                    "",
                ]
                if file_month_summaries:
                    for summary in file_month_summaries:
                        target = _markdown_relative_url(
                            "文件备份/" + summary["name"]
                        )
                        file_index_lines.append(
                            f"- [{summary['month']}](<{target}>) · "
                            f"{summary['ready']} 条可打开 · "
                            f"{summary['pending']} 条待解析"
                        )
                else:
                    file_index_lines.append("当前没有文件记录。")
                actual_file_chat, file_chat_changed = self._write_managed_text(
                    os.path.join(chat_root, "00-文件备份.md"),
                    "\n".join(file_index_lines),
                    target_view=True,
                )
                if file_chat_changed:
                    written += 1
                managed_paths.append(actual_file_chat)
                file_chat_name = os.path.basename(actual_file_chat)
                index_lines[4:4] = [
                    f"> [只看文件备份](<{_markdown_relative_url(file_chat_name)}>)",
                    "",
                ]
            _actual_index_path, index_changed = self._write_managed_text(
                os.path.join(chat_root, "00-资源索引.md"),
                "\n".join(index_lines),
                target_view=target_view,
            )
            if index_changed:
                written += 1
            managed_paths.append(_actual_index_path)
            chat_summaries.append({
                "alias": _single_line(chat_alias, "未命名群聊", 120),
                "path": os.path.relpath(_actual_index_path, base_root).replace(
                    os.sep, "/"
                ),
                "months": len(months),
                "links": sum(
                    1
                    for rows in months.values()
                    for row in rows
                    if row.get("kind") == "link"
                ),
                "files": sum(
                    1
                    for rows in months.values()
                    for row in rows
                    if row.get("kind") == "file"
                ),
                "file_path": (
                    os.path.relpath(actual_file_chat, base_root).replace(
                        os.sep,
                        "/",
                    )
                    if actual_file_chat else ""
                ),
                "ready_files": ready_files,
                "pending_files": pending_files,
                "file_months": len(file_month_summaries),
            })

        file_scope_path = ""
        if target_view:
            file_chat_summaries = [
                item for item in chat_summaries if item["file_path"]
            ]
            file_scope_lines = [
                INDEX_MARKER,
                "# 文件备份",
                "",
                "> 文件专属入口：按群聊进入，只显示附件，不混入网页链接。",
                "",
            ]
            if file_chat_summaries:
                for summary in sorted(
                    file_chat_summaries,
                    key=lambda item: (item["alias"].casefold(), item["file_path"]),
                ):
                    label = _markdown_label(summary["alias"], "未命名群聊")
                    target = _markdown_relative_url(summary["file_path"])
                    file_scope_lines.append(
                        f"- [{label}](<{target}>) · "
                        f"{summary['ready_files']} 条可打开 · "
                        f"{summary['pending_files']} 条待解析 · "
                        f"{summary['file_months']} 个月份"
                    )
            else:
                file_scope_lines.append("当前没有已选群聊文件。")
            file_scope_path, file_scope_changed = self._write_managed_text(
                os.path.join(base_root, "00-文件备份.md"),
                "\n".join(file_scope_lines),
                target_view=True,
            )
            if file_scope_changed:
                written += 1
            managed_paths.append(file_scope_path)
        scope_lines = [
            INDEX_MARKER,
            "# 资源索引",
            "",
            "> 按群聊进入链接与文件清单。",
            "",
        ]
        if target_view:
            file_scope_name = os.path.basename(file_scope_path)
            scope_lines.extend([
                f"> [只看文件备份](<{_markdown_relative_url(file_scope_name)}>)",
                "",
            ])
        if chat_summaries:
            for summary in sorted(
                chat_summaries,
                key=lambda item: (item["alias"].casefold(), item["path"]),
            ):
                label = _markdown_label(summary["alias"], "未命名群聊")
                if target_view:
                    target = _markdown_relative_url(summary["path"])
                    link = f"[{label}](<{target}>)"
                else:
                    target = os.path.splitext(summary["path"])[0]
                    link = f"[[{target}|{label}]]"
                scope_lines.append(
                    f"- {link} · {summary['links']} 个链接 · "
                    f"{summary['files']} 个文件 · {summary['months']} 个月份"
                )
        else:
            scope_lines.append("当前没有已选群聊资源。")
        scope_path, scope_changed = self._write_managed_text(
            os.path.join(base_root, "00-资源索引.md"),
            "\n".join(scope_lines),
            target_view=target_view,
        )
        if scope_changed:
            written += 1
        managed_paths.append(scope_path)
        self._reconcile_managed_projection(
            base_root,
            managed_paths,
            target_view=target_view,
            previous_paths=previous_managed_paths,
        )
        return written

    def render_obsidian_indexes(self):
        try:
            with self.capture.canonical_operation():
                with self._worker_lock():
                    self._ensure_schema()
                    with self._projection_worker_lock():
                        return self._render_obsidian_indexes_owned()
        except (ResourceBackupError, ResourceCaptureError) as exc:
            if exc.code in {"worker_busy", "capture_worker_busy"}:
                return {
                    "state": "worker_busy",
                    "files_written": 0,
                    "occurrences": 0,
                    "error_code": exc.code,
                }
            if isinstance(exc, ResourceCaptureError):
                return {
                    "state": exc.code,
                    "files_written": 0,
                    "occurrences": 0,
                    "error_code": exc.code,
                }
            raise

    def _render_obsidian_indexes_owned(self):
        occurrences = self.capture.occurrences(selected_only=True)
        root = self.obsidian_projection_root
        self._ensure_projection_dir(root)
        return {
            "state": "written",
            "files_written": self._render_indexes_at(root, occurrences, target_view=False),
            "occurrences": len(occurrences),
        }

    def _render_obsidian_indexes_safely(self):
        try:
            with self._projection_worker_lock():
                return self._render_obsidian_indexes_owned()
        except (OSError, ResourceBackupError) as exc:
            return {
                "state": (
                    "worker_busy"
                    if str(getattr(exc, "code", "")) == "worker_busy"
                    else "projection_failed"
                ),
                "files_written": 0,
                "occurrences": len(self.capture.occurrences(selected_only=True)),
                "error_code": str(
                    getattr(exc, "code", "") or type(exc).__name__
                ),
            }

    def _render_target_indexes(self, occurrences):
        root = os.path.join(self.backup_root, "views")
        self._ensure_target_dir(root)
        written = self._render_indexes_at(root, occurrences, target_view=True)
        delivery_map = self._delivery_map()
        ready, pending = self._file_delivery_counts(occurrences, delivery_map)
        ready_digests = {
            str(item.get("object_sha256") or "")
            for item in occurrences
            if self._ready_delivery(item, delivery_map)
        }
        file_scope_path = self._managed_text_path(
            os.path.join(root, "00-文件备份.md"),
            target_view=True,
        )
        resource_scope_path = self._managed_text_path(
            os.path.join(root, "00-资源索引.md"),
            target_view=True,
        )
        portal_lines = [
            PORTAL_MARKER,
            "# 微信资源备份 / WeChat Resource Backup",
            "",
            f"**{ready} 条可打开（{len(ready_digests)} 个去重文件） · {pending} 条待解析**",
            "",
            f"- [打开文件备份](<{_markdown_relative_url(os.path.relpath(file_scope_path, self.target_namespace_root).replace(os.sep, '/'))}>)",
            f"- [打开链接与文件总索引](<{_markdown_relative_url(os.path.relpath(resource_scope_path, self.target_namespace_root).replace(os.sep, '/'))}>)",
            "",
            "> “可打开”表示文件已经交付到当前 mounted destination；不表示 Google Drive、Dropbox 或 iCloud 已完成远端同步。",
            "",
            "`v3/objects` 与 `v3/snapshots` 是系统目录，请从上面的文件入口浏览，不要手工整理或改名。",
            "",
            "待解析项目只有 metadata；需要在长驻 app 的本次会话中显式允许附件解析，才会读取并备份文件 bytes。",
        ]
        _portal_path, portal_changed = self._write_target_portal(
            "\n".join(portal_lines),
        )
        return written + int(portal_changed)

    def plan(self):
        try:
            with self.capture.canonical_operation():
                with self._worker_lock():
                    self._ensure_schema()
                    return self._plan_owned()
        except (ResourceBackupError, ResourceCaptureError) as exc:
            if exc.code in {"worker_busy", "capture_worker_busy"}:
                return {
                    "state": "worker_busy",
                    "error_code": exc.code,
                    "selected_chats": 0,
                    "occurrences": 0,
                    "links": 0,
                    "file_occurrences": 0,
                    "objects": 0,
                    "pending_objects": 0,
                    "unresolved_files": 0,
                }
            if isinstance(exc, ResourceCaptureError):
                return {
                    "state": exc.code,
                    "error_code": exc.code,
                    "selected_chats": 0,
                    "occurrences": 0,
                    "links": 0,
                    "file_occurrences": 0,
                    "objects": 0,
                    "pending_objects": 0,
                    "unresolved_files": 0,
                }
            raise

    def _plan_owned(self):
        occurrences = self.capture.occurrences(selected_only=True)
        objects = self._object_rows(occurrences)
        boundary = self._target_boundary_error(occurrences, objects)
        delivery_map = self._delivery_map()
        pending = 0
        for row in objects:
            digest = str(row["object_sha256"])
            delivery = delivery_map.get(digest)
            if not delivery or delivery["status"] != "sync_delegated":
                pending += 1
                continue
            target_path = os.path.join(
                self.backup_root, str(delivery.get("target_relpath") or "")
            )
            if not _within(target_path, self.backup_root) or not os.path.lexists(target_path):
                pending += 1
                continue
            try:
                target_stat = os.lstat(target_path)
            except OSError:
                pending += 1
                continue
            if (
                stat.S_ISLNK(target_stat.st_mode)
                or not stat.S_ISREG(target_stat.st_mode)
                or int(target_stat.st_size) != int(row["object_size"])
            ):
                pending += 1
        if boundary:
            state = "invalid_target"
        elif not self.target:
            state = "target_not_configured"
        elif not os.path.isdir(self.target) or not os.access(self.target, os.W_OK):
            state = "destination_unavailable"
        elif not self.capture.selected_chats():
            state = "no_selected_chats"
        else:
            state = "ready"
        return {
            "state": state,
            "error_code": boundary,
            "selected_chats": len(self.capture.selected_chats()),
            "occurrences": len(occurrences),
            "links": sum(1 for row in occurrences if row["kind"] == "link"),
            "file_occurrences": sum(1 for row in occurrences if row["kind"] == "file"),
            "objects": len(objects),
            "pending_objects": pending,
            "unresolved_files": sum(
                1 for row in occurrences
                if row["kind"] == "file" and row["status"] != "ready_local"
            ),
        }

    def run(self):
        obsidian = {
            "state": "not_run_worker_busy",
            "files_written": 0,
            "occurrences": 0,
        }
        try:
            with self.capture.canonical_operation():
                with self._worker_lock():
                    self._ensure_schema()
                    return self._run_owned()
        except (ResourceBackupError, ResourceCaptureError, ArchiveError, OSError) as exc:
            if str(getattr(exc, "code", "")) in {
                "worker_busy",
                "capture_worker_busy",
            }:
                return {
                    "state": "worker_busy",
                    "copied": 0,
                    "failed": 0,
                    "obsidian": obsidian,
                }
            code = str(getattr(exc, "code", "") or type(exc).__name__)
            if isinstance(exc, ResourceCaptureError):
                return {
                    "state": code,
                    "error_code": code,
                    "copied": 0,
                    "reused": 0,
                    "failed": 0,
                    "obsidian": obsidian,
                }
            return {
                "state": "target_failed",
                "copied": 0,
                "reused": 0,
                "failed": 1,
                "error_codes": [code],
                "obsidian": obsidian,
            }

    def _run_owned(self):
        """Render and hand off while holding the cross-process operation lock."""
        obsidian = self._render_obsidian_indexes_safely()
        if str(obsidian.get("state") or "") not in {"written", "unchanged"}:
            return {
                "state": str(obsidian.get("state") or "projection_failed"),
                "copied": 0,
                "reused": 0,
                "failed": 0,
                "obsidian": obsidian,
            }
        if not self.target:
            return {
                "state": "target_not_configured",
                "copied": 0,
                "failed": 0,
                "obsidian": obsidian,
            }
        occurrences = self.capture.occurrences(selected_only=True)
        object_rows = self._object_rows(occurrences)
        boundary = self._target_boundary_error(occurrences, object_rows)
        if boundary:
            state = (
                "target_failed"
                if boundary == "target_subtree_conflict"
                else "invalid_target"
            )
            return {
                "state": state,
                "error_code": boundary,
                "error_codes": [boundary] if state == "target_failed" else [],
                "copied": 0,
                "failed": 0,
                "obsidian": obsidian,
            }
        if not os.path.isdir(self.target) or not os.access(self.target, os.W_OK):
            return {
                "state": "destination_unavailable",
                "copied": 0,
                "failed": 0,
                "obsidian": obsidian,
            }
        if not self.capture.selected_chats():
            target_indexes = 0
            if self._read_destination_marker(required=False) is not None:
                with self._target_worker_lock():
                    self._ensure_destination_identity_owned()
                    target_indexes = self._render_target_indexes([])
            return {
                "state": "no_selected_chats",
                "copied": 0,
                "failed": 0,
                "obsidian": obsidian,
                "target_index_files_written": target_indexes,
            }
        with self._target_worker_lock():
            self._ensure_destination_identity_owned()
            return self._run_locked(
                obsidian=obsidian,
                occurrences=occurrences,
                object_rows=object_rows,
            )

    def _run_locked(self, *, obsidian, occurrences, object_rows):
        self._ensure_target_dir(self.backup_root)
        copied = 0
        reused = 0
        failed = 0
        error_codes = []
        for row in object_rows:
            try:
                state, _relpath = self._copy_object(row)
                if state == "copied":
                    copied += 1
                else:
                    reused += 1
            except (ResourceBackupError, ArchiveError, OSError) as exc:
                failed += 1
                error_codes.append(str(getattr(exc, "code", "") or type(exc).__name__))
        if failed:
            return {
                "state": "target_failed",
                "copied": copied,
                "reused": reused,
                "failed": failed,
                "error_codes": sorted(set(error_codes)),
                "obsidian": obsidian,
            }

        delivery_map = self._delivery_map()
        records = self._catalog_records(occurrences, delivery_map)
        target_indexes = self._render_target_indexes(occurrences)
        snapshot = self._write_snapshot(records, object_rows, delivery_map)
        unresolved_files = int(snapshot.get("unresolved_files") or 0)
        if unresolved_files:
            state = "pending_resources"
        elif copied or snapshot["state"] == "written":
            state = "sync_delegated"
        else:
            state = "idle"
        return {
            "state": state,
            "remote_verified": False,
            "copied": copied,
            "reused": reused,
            "failed": 0,
            "snapshot": snapshot,
            "target_index_files_written": target_indexes,
            "obsidian": obsidian,
            "unresolved_files": unresolved_files,
        }

    def _snapshot_dir(self, snapshot_id=""):
        if snapshot_id:
            return os.path.join(
                self.backup_root, "snapshots", os.path.basename(snapshot_id)
            )
        state = self._state_row()
        if state is None or not state["snapshot_id"]:
            return ""
        return os.path.join(self.backup_root, "snapshots", state["snapshot_id"])

    def _load_snapshot(self, snapshot_id=""):
        directory = self._snapshot_dir(snapshot_id)
        if not directory:
            return None
        try:
            with open(os.path.join(directory, "COMPLETE"), "rb") as handle:
                complete_bytes = handle.read()
            with open(os.path.join(directory, "manifest.json"), "rb") as handle:
                manifest_bytes = handle.read()
            with open(os.path.join(directory, "resources.jsonl"), "rb") as handle:
                resources_bytes = handle.read()
            complete = json.loads(complete_bytes.decode("utf-8"))
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if (
            complete.get("schema") != BACKUP_SCHEMA
            or complete.get("state") != "complete"
            or manifest.get("schema") != BACKUP_SCHEMA
            or complete.get("snapshot_id") != manifest.get("snapshot_id")
            or complete.get("manifest_sha256") != hashlib.sha256(manifest_bytes).hexdigest()
            or complete.get("resources_sha256") != hashlib.sha256(resources_bytes).hexdigest()
            or manifest.get("resources_sha256") != hashlib.sha256(resources_bytes).hexdigest()
        ):
            return None
        return {"directory": directory, "manifest": manifest}

    def verify(self, snapshot_id=""):
        try:
            with self.capture.canonical_operation():
                with self._worker_lock():
                    self._ensure_schema()
                    return self._verify_owned(snapshot_id)
        except (ResourceBackupError, ResourceCaptureError) as exc:
            if exc.code in {"worker_busy", "capture_worker_busy"}:
                return {
                    "state": "worker_busy",
                    "verified": 0,
                    "failed": 0,
                    "error_code": exc.code,
                }
            if isinstance(exc, ResourceCaptureError):
                return {
                    "state": exc.code,
                    "verified": 0,
                    "failed": 0,
                    "error_code": exc.code,
                }
            raise

    def _verify_owned(self, snapshot_id=""):
        snapshot = self._load_snapshot(snapshot_id)
        if snapshot is None:
            return {"state": "snapshot_unavailable", "verified": 0, "failed": 0}
        verified = 0
        failed = 0
        for row in snapshot["manifest"].get("objects") or []:
            digest = str(row.get("sha256") or "")
            relpath = str(row.get("target_relpath") or "")
            path = os.path.join(self.backup_root, relpath)
            if not relpath or not _within(path, self.backup_root):
                failed += 1
                continue
            try:
                size, actual = _hash_path(path)
            except (OSError, ResourceBackupError):
                failed += 1
                continue
            if size == int(row.get("size") or -1) and actual == digest:
                verified += 1
            else:
                failed += 1
        return {
            "state": "target_verified" if failed == 0 else "target_failed",
            "snapshot_id": snapshot["manifest"]["snapshot_id"],
            "verified": verified,
            "failed": failed,
            "remote_verified": False,
        }

    def status(self):
        plan = self.plan()
        if not self._schema_ready:
            return {
                **plan,
                "latest_snapshot": "",
                "handoff_semantics": str(plan.get("state") or "unavailable"),
                "remote_verified": False,
                "link_export_mode": self.link_export_mode,
            }
        state = self._state_row() if self.destination_id else None
        plan_state = str(plan.get("state") or "")
        if plan_state != "ready":
            handoff_semantics = plan_state or "pending"
        elif int(plan.get("unresolved_files") or 0):
            handoff_semantics = "pending_resources"
        elif int(plan.get("pending_objects") or 0):
            handoff_semantics = "pending"
        else:
            occurrences = self.capture.occurrences(selected_only=True)
            delivery_map = self._delivery_map()
            records = self._catalog_records(occurrences, delivery_map)
            resources_sha256 = hashlib.sha256(
                _canonical_jsonl_bytes(records)
            ).hexdigest()
            snapshot = (
                self._load_snapshot(str(state["snapshot_id"])) if state else None
            )
            manifest = snapshot["manifest"] if snapshot else {}
            delegated = (
                state is not None
                and str(state["catalog_sha256"]) == resources_sha256
                and manifest.get("resources_sha256") == resources_sha256
                and manifest.get("link_export_mode") == self.link_export_mode
                and manifest.get("handoff_semantics") == "sync_delegated"
            )
            handoff_semantics = "sync_delegated" if delegated else "pending"
        return {
            **plan,
            "latest_snapshot": str(state["snapshot_id"]) if state else "",
            "handoff_semantics": handoff_semantics,
            "remote_verified": False,
            "link_export_mode": self.link_export_mode,
        }
