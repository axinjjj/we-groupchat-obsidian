"""Read-only evidence model for exact stale relation Markdown cleanup."""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import hashlib
import json
import math
import os
import secrets
import signal
import sqlite3
import stat
import sys
import threading
import time
import unicodedata
from urllib.parse import quote

from . import knowledge as knowledge_module
from .knowledge import KnowledgeStore, _render_relation_markdown_line
from .relation_audit import (
    KNOWN_BROKEN_RELATION_REASON,
    RISKY_CROSS_CHAT_CONDITION_SQL,
    _integrity_check,
)


MANIFEST_SCHEMA = "exact_relation_markdown_cleanup/v1"
VERIFIED_BACKUP_SHA256 = "8bc0ee22c1fb94ecff6bf936e5bfb2f7792d853702b9b63c42ca5c1378d2b7eb"
APPLY_TOKEN_PREFIX = "APPLY_EXACT_RELATION_MARKDOWN:"
ROLLBACK_TOKEN_PREFIX = "ROLLBACK_EXACT_RELATION_MARKDOWN:"
STATE_SCHEMA = "exact_relation_markdown_cleanup/state-v1"
RELATION_SECTION_HEADING = "## \u76f8\u5173\u4e3b\u9898\n".encode("utf-8")
RELATION_SECTION_PREFIX = "## \u76f8\u5173\u4e3b\u9898".encode("utf-8")

INVALID_EDGE_SQL = """
SELECT
    r.relation_id,
    r.source_topic_id,
    r.target_topic_id,
    s.topic_key AS source_topic_key,
    s.title AS source_title,
    s.obsidian_path AS source_path,
    t.title AS target_title,
    t.obsidian_path AS target_path
FROM relations r
JOIN topics s ON s.topic_id = r.source_topic_id
JOIN topics t ON t.topic_id = r.target_topic_id
WHERE r.relation = 'updates' AND r.reason = ?
ORDER BY r.relation_id
"""

CURRENT_RELATION_SQL = """
SELECT relation_id, source_topic_id, target_topic_id, relation, reason, created_at
FROM relations
ORDER BY relation_id
"""


class CleanupError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = str(code)


@dataclass(frozen=True)
class CleanupExpectations:
    backup_sha256: str
    selected_edges: int
    self_loops: int
    renderable_edges: int


LIVE_EXPECTATIONS = CleanupExpectations(
    backup_sha256=VERIFIED_BACKUP_SHA256,
    selected_edges=1774,
    self_loops=2,
    renderable_edges=1772,
)


def canonical_json_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def open_read_only_db(path, *, immutable=False):
    absolute = os.path.abspath(os.path.expanduser(str(path)))
    suffix = "&immutable=1" if immutable else ""
    conn = sqlite3.connect(f"file:{quote(absolute)}?mode=ro{suffix}", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_integer(value, *, code, field):
    if type(value) is not int:
        raise CleanupError(code, f"{field} must be a SQLite INTEGER")
    return value


def _require_text(value, *, code, field, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise CleanupError(code, f"{field} must be SQLite TEXT")
    return value


def _timestamp_17g(value, *, code):
    if type(value) not in (int, float):
        raise CleanupError(code, "created_at must be a SQLite INTEGER or REAL")
    number = float(value)
    if not math.isfinite(number):
        raise CleanupError(code, "created_at must be finite")
    return format(number, ".17g")


def _relation_tuple(row, *, code):
    relation_id = _require_integer(row["relation_id"], code=code, field="relation_id")
    source_topic_id = _require_integer(
        row["source_topic_id"], code=code, field="source_topic_id"
    )
    target_topic_id = _require_integer(
        row["target_topic_id"], code=code, field="target_topic_id"
    )
    relation = _require_text(row["relation"], code=code, field="relation")
    reason = _require_text(row["reason"], code=code, field="reason")
    return (
        relation_id,
        source_topic_id,
        target_topic_id,
        relation,
        sha256_bytes(reason.encode("utf-8")),
        _timestamp_17g(row["created_at"], code=code),
    )


def _validate_selected_edge(row):
    code = "backup_value_type"
    _require_integer(row["relation_id"], code=code, field="relation_id")
    _require_integer(row["source_topic_id"], code=code, field="source_topic_id")
    _require_integer(row["target_topic_id"], code=code, field="target_topic_id")
    _require_text(
        row["source_topic_key"],
        code=code,
        field="source_topic_key",
        nullable=True,
    )
    for field in ("source_title", "source_path", "target_title", "target_path"):
        _require_text(row[field], code=code, field=field)


def _validate_source_identity(row, *, code):
    if row is None:
        return
    _require_integer(row["topic_id"], code=code, field="topic_id")
    _require_text(row["topic_key"], code=code, field="topic_key", nullable=True)
    _require_text(row["title"], code=code, field="title")
    _require_text(row["obsidian_path"], code=code, field="obsidian_path")


def _validate_target_identity(row, *, code):
    if row is None:
        return
    _require_integer(row["topic_id"], code=code, field="topic_id")
    _require_text(row["title"], code=code, field="title")
    _require_text(row["obsidian_path"], code=code, field="obsidian_path")


def _canonical_digest(value, *, code):
    try:
        return sha256_bytes(canonical_json_bytes(value))
    except (TypeError, ValueError) as exc:
        raise CleanupError(code, "canonical evidence encoding failed") from exc


def _count(conn, sql, parameters=()):
    return int(conn.execute(sql, parameters).fetchone()[0])


def _database_counts(conn):
    return {
        "topics": _count(conn, "SELECT COUNT(*) FROM topics"),
        "events": _count(conn, "SELECT COUNT(*) FROM events"),
        "fts": _count(conn, "SELECT COUNT(*) FROM topic_fts"),
        "orphan_events": _count(
            conn,
            """
            SELECT COUNT(*)
            FROM events e
            LEFT JOIN topics t ON t.topic_id = e.topic_id
            WHERE t.topic_id IS NULL
            """,
        ),
        "orphan_relations": _count(
            conn,
            """
            SELECT COUNT(*)
            FROM relations r
            LEFT JOIN topics s ON s.topic_id = r.source_topic_id
            LEFT JOIN topics t ON t.topic_id = r.target_topic_id
            WHERE s.topic_id IS NULL OR t.topic_id IS NULL
            """,
        ),
    }


def _fts_parity_evidence(conn):
    return {
        "row_count": _count(conn, "SELECT COUNT(*) FROM topic_fts"),
        "missing_topic_count": _count(
            conn,
            """
            SELECT COUNT(*)
            FROM topics t
            WHERE NOT EXISTS (
                SELECT 1
                FROM topic_fts f
                WHERE typeof(f.topic_id) = 'integer'
                  AND f.topic_id = t.topic_id
            )
            """,
        ),
        "orphan_topic_id_count": _count(
            conn,
            """
            SELECT COUNT(*)
            FROM topic_fts f
            LEFT JOIN topics t ON t.topic_id = f.topic_id
            WHERE typeof(f.topic_id) = 'integer'
              AND t.topic_id IS NULL
            """,
        ),
        "duplicate_topic_id_count": _count(
            conn,
            """
            SELECT COUNT(*)
            FROM (
                SELECT f.topic_id
                FROM topic_fts f
                WHERE typeof(f.topic_id) = 'integer'
                GROUP BY f.topic_id
                HAVING COUNT(*) != 1
            ) duplicates
            """,
        ),
        "null_topic_id_count": _count(
            conn,
            "SELECT COUNT(*) FROM topic_fts WHERE topic_id IS NULL",
        ),
        "noninteger_topic_id_count": _count(
            conn,
            """
            SELECT COUNT(*)
            FROM topic_fts
            WHERE topic_id IS NOT NULL
              AND typeof(topic_id) != 'integer'
            """,
        ),
    }


def _topic_by_id(conn, topic_id):
    return conn.execute(
        """
        SELECT topic_id, topic_key, title, obsidian_path
        FROM topics
        WHERE topic_id = ?
        """,
        (topic_id,),
    ).fetchone()


def _path_owner_count(conn, path):
    return _count(
        conn,
        "SELECT COUNT(*) FROM topics WHERE obsidian_path = ?",
        (path,),
    )


def _source_identity_tuple(row):
    return (
        row["topic_id"],
        row["topic_key"],
        row["title"],
        row["obsidian_path"],
    )


def _target_identity_tuple(row):
    return (
        row["topic_id"],
        row["title"],
        row["obsidian_path"],
    )


def read_file_bounded(path, timeout_seconds=30):
    """Read a file without exposing its path through stable public errors."""
    def _read():
        with open(path, "rb") as handle:
            return handle.read()

    return _run_bounded_read(_read, timeout_seconds)


def _restore_prior_alarm(previous_handler, previous_timer, started_at):
    elapsed = max(0.0, time.monotonic() - started_at)
    previous_remaining, previous_interval = previous_timer
    if previous_remaining > 0:
        adjusted_remaining = max(0.0, previous_remaining - elapsed)
        if adjusted_remaining == 0:
            adjusted_remaining = 1e-6
    else:
        adjusted_remaining = 0.0
    failures = []

    def _attempt(operation):
        try:
            operation()
            return True
        except (OSError, ValueError) as exc:
            failures.append(exc)
            return False

    _attempt(lambda: signal.setitimer(signal.ITIMER_REAL, 0))
    if not _attempt(lambda: signal.signal(signal.SIGALRM, previous_handler)):
        _attempt(lambda: signal.signal(signal.SIGALRM, previous_handler))
    if not _attempt(
        lambda: signal.setitimer(
            signal.ITIMER_REAL, adjusted_remaining, previous_interval
        )
    ):
        _attempt(
            lambda: signal.setitimer(
                signal.ITIMER_REAL, adjusted_remaining, previous_interval
            )
        )
    if failures:
        raise CleanupError(
            "materialization_timer_restore", "bounded read timer restoration failed"
        ) from failures[0]


def _run_bounded_read(reader, timeout_seconds):
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    started_at = time.monotonic()

    def _timeout(_signum, _frame):
        raise TimeoutError("bounded materialization read timed out")

    try:
        signal.signal(signal.SIGALRM, _timeout)
        signal.setitimer(signal.ITIMER_REAL, float(timeout_seconds))
        try:
            return reader()
        except TimeoutError as exc:
            raise CleanupError(
                "materialization_timeout", "candidate materialization timed out"
            ) from exc
        except OSError as exc:
            raise CleanupError(
                "materialization_unreadable", "candidate materialization is unreadable"
            ) from exc
    finally:
        _restore_prior_alarm(previous_handler, previous_timer, started_at)


def _read_fd_bounded(descriptor, timeout_seconds=30):
    try:
        worker_descriptor = os.dup(descriptor)
    except OSError as exc:
        raise CleanupError(
            "materialization_unreadable", "candidate materialization is unreadable"
        ) from exc

    completed = threading.Event()
    result = {}

    def _read():
        try:
            os.lseek(worker_descriptor, 0, os.SEEK_SET)
            chunks = []
            while True:
                chunk = os.read(worker_descriptor, 1024 * 1024)
                if not chunk:
                    result["data"] = b"".join(chunks)
                    return
                chunks.append(chunk)
        except Exception as exc:
            result["error"] = exc
        finally:
            os.close(worker_descriptor)
            completed.set()

    worker = threading.Thread(
        target=_read,
        name="relation-markdown-bounded-read",
        daemon=True,
    )
    try:
        worker.start()
    except Exception as exc:
        os.close(worker_descriptor)
        raise CleanupError(
            "materialization_unreadable", "candidate materialization is unreadable"
        ) from exc
    if not completed.wait(float(timeout_seconds)):
        # The worker exclusively owns its duplicated descriptor.  Leave it open
        # until the blocked syscall returns so a late loop iteration can never
        # observe a descriptor number reused for another file.  The daemon does
        # not keep the refusing CLI process alive.
        raise CleanupError(
            "materialization_timeout", "candidate materialization timed out"
        )

    error = result.get("error")
    if isinstance(error, TimeoutError):
        raise CleanupError(
            "materialization_timeout", "candidate materialization timed out"
        ) from error
    if isinstance(error, OSError):
        raise CleanupError(
            "materialization_unreadable", "candidate materialization is unreadable"
        ) from error
    if error is not None:
        raise error
    try:
        return result["data"]
    except KeyError as exc:
        raise CleanupError(
            "materialization_unreadable", "candidate materialization is unreadable"
        ) from exc


def _normalized_relative_path(value, *, code):
    if not isinstance(value, str) or not value or "\\" in value:
        raise CleanupError(code, "path is not a normalized relative path")
    if os.path.isabs(value):
        raise CleanupError(code, "path is not a normalized relative path")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise CleanupError(code, "path is not a normalized relative path")
    if os.path.normpath(value) != value:
        raise CleanupError(code, "path is not a normalized relative path")
    return parts


def _is_protected_topic_path(parts):
    basename = parts[-1]
    folded_parts = [unicodedata.normalize("NFC", part).casefold() for part in parts]
    folded_basename = folded_parts[-1]
    if basename in {"00-\u6309\u65e5\u671f.md", "00-\u6309\u65e5\u671f.generated.md", "\u76ee\u5f55.md"}:
        return True
    if "\u6309\u65e5\u671f" in parts[:-1]:
        return True
    protected_tokens = ("maintenance", "digest")
    if any(token in part for part in folded_parts for token in protected_tokens):
        return True
    if folded_basename in {"index.md", "daily digest.md"}:
        return True
    return False


def validate_topic_path(vault_root, obsidian_subdir, relative_path):
    """Return the regular one-link absolute path or raise CleanupError."""
    root = os.path.realpath(os.path.abspath(os.path.expanduser(str(vault_root))))
    subdir_parts = _normalized_relative_path(
        str(obsidian_subdir), code="obsidian_subdir_invalid"
    )
    path_parts = _normalized_relative_path(relative_path, code="topic_path_invalid")
    if path_parts[: len(subdir_parts)] != subdir_parts:
        raise CleanupError("topic_path_outside_subdir", "topic path is outside scope")
    if not relative_path.endswith(".md"):
        raise CleanupError("topic_path_not_markdown", "topic path is not Markdown")
    if _is_protected_topic_path(path_parts):
        raise CleanupError("protected_topic_path", "topic path is protected")

    candidate = os.path.abspath(os.path.join(root, *path_parts))
    try:
        if os.path.commonpath((root, candidate)) != root:
            raise CleanupError("topic_path_escape", "topic path escapes vault root")
    except ValueError as exc:
        raise CleanupError("topic_path_escape", "topic path escapes vault root") from exc

    current = root
    try:
        for part in path_parts:
            current = os.path.join(current, part)
            info = os.lstat(current)
            if stat.S_ISLNK(info.st_mode):
                raise CleanupError("topic_path_symlink", "topic path has a symlink")
        info = os.lstat(candidate)
    except FileNotFoundError as exc:
        raise CleanupError("topic_file_missing", "topic file is missing") from exc
    except OSError as exc:
        raise CleanupError("topic_file_unreadable", "topic file is unreadable") from exc
    if not stat.S_ISREG(info.st_mode):
        raise CleanupError("topic_file_nonregular", "topic file is not regular")
    if info.st_nlink != 1:
        raise CleanupError("topic_file_hardlinked", "topic file has multiple links")
    if stat.S_IMODE(info.st_mode) & 0o444 == 0:
        raise CleanupError("topic_file_unreadable", "topic file is unreadable")
    return candidate


def _directory_open_flags():
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _candidate_open_flags():
    return (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _map_candidate_open_error(exc, *, final=False):
    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
        raise CleanupError("topic_path_symlink", "topic path changed or is symlinked") from exc
    if exc.errno == errno.ENOENT:
        code = "topic_file_missing" if final else "topic_path_race"
        raise CleanupError(code, "topic path changed during validation") from exc
    raise CleanupError("topic_file_unreadable", "topic file is unreadable") from exc


def _file_identity(info):
    return (info.st_dev, info.st_ino)


def _verify_candidate_identity_chain(
    root_fd, path_parts, directory_identities, candidate_identity
):
    current_fd = os.dup(root_fd)
    try:
        if _file_identity(os.fstat(current_fd)) != directory_identities[0]:
            raise CleanupError("topic_identity_race", "vault root identity changed")
        for index, part in enumerate(path_parts[:-1], start=1):
            try:
                next_fd = os.open(part, _directory_open_flags(), dir_fd=current_fd)
            except OSError as exc:
                raise CleanupError(
                    "topic_identity_race", "topic directory identity changed"
                ) from exc
            os.close(current_fd)
            current_fd = next_fd
            if _file_identity(os.fstat(current_fd)) != directory_identities[index]:
                raise CleanupError(
                    "topic_identity_race", "topic directory identity changed"
                )
        try:
            check_fd = os.open(
                path_parts[-1], _candidate_open_flags(), dir_fd=current_fd
            )
        except OSError as exc:
            raise CleanupError(
                "topic_identity_race", "topic file identity changed"
            ) from exc
        try:
            check_info = os.fstat(check_fd)
            if (
                _file_identity(check_info) != candidate_identity
                or not stat.S_ISREG(check_info.st_mode)
                or check_info.st_nlink != 1
                or stat.S_IMODE(check_info.st_mode) & 0o444 == 0
            ):
                raise CleanupError("topic_identity_race", "topic file identity changed")
        finally:
            os.close(check_fd)
    finally:
        os.close(current_fd)


def _read_candidate_from_pinned_root(
    vault_root,
    obsidian_subdir,
    relative_path,
    timeout_seconds=30,
    *,
    _include_identity_chain=False,
):
    root = os.path.abspath(os.path.expanduser(str(vault_root)))
    path_parts = _normalized_relative_path(relative_path, code="topic_path_invalid")
    try:
        root_fd = os.open(root, _directory_open_flags())
    except OSError as exc:
        raise CleanupError("vault_root_unavailable", "vault root is unavailable") from exc
    try:
        root_info = os.fstat(root_fd)
        directory_identities = [_file_identity(root_info)]
        validate_topic_path(root, obsidian_subdir, relative_path)
        current_fd = os.dup(root_fd)
        try:
            for part in path_parts[:-1]:
                try:
                    next_fd = os.open(part, _directory_open_flags(), dir_fd=current_fd)
                except OSError as exc:
                    _map_candidate_open_error(exc)
                os.close(current_fd)
                current_fd = next_fd
                directory_identities.append(_file_identity(os.fstat(current_fd)))
            try:
                candidate_fd = os.open(
                    path_parts[-1], _candidate_open_flags(), dir_fd=current_fd
                )
            except OSError as exc:
                _map_candidate_open_error(exc, final=True)
            try:
                info = os.fstat(candidate_fd)
                if not stat.S_ISREG(info.st_mode):
                    raise CleanupError("topic_file_nonregular", "topic file is not regular")
                if info.st_nlink != 1:
                    raise CleanupError("topic_file_hardlinked", "topic file has multiple links")
                if stat.S_IMODE(info.st_mode) & 0o444 == 0:
                    raise CleanupError("topic_file_unreadable", "topic file is unreadable")
                data = _read_fd_bounded(candidate_fd, timeout_seconds)
                _verify_candidate_identity_chain(
                    root_fd,
                    path_parts,
                    directory_identities,
                    _file_identity(info),
                )
                try:
                    current_root_info = os.stat(root, follow_symlinks=False)
                except OSError as exc:
                    raise CleanupError(
                        "vault_root_race", "vault root identity changed"
                    ) from exc
                if (
                    stat.S_ISLNK(current_root_info.st_mode)
                    or (current_root_info.st_dev, current_root_info.st_ino)
                    != (root_info.st_dev, root_info.st_ino)
                ):
                    raise CleanupError("vault_root_race", "vault root identity changed")
                # This observation is an immutable CAS preimage, not a filesystem
                # snapshot. Task 4 must revalidate pre_sha256 before any write; a
                # later same-inode rewrite is apply-time drift, not a reason to add
                # an inherently racy second-read loop here.
                result = (data, info)
                if _include_identity_chain:
                    return (
                        *result,
                        tuple([*directory_identities, _file_identity(info)]),
                    )
                return result
            finally:
                os.close(candidate_fd)
        finally:
            os.close(current_fd)
    finally:
        os.close(root_fd)


def relation_section_line_indexes(data):
    """Return line indexes inside the unique exact LF relation section."""
    if not isinstance(data, bytes):
        raise CleanupError("materialization_type", "materialization must be bytes")
    lines = data.splitlines(keepends=True)
    malformed = [
        line
        for line in lines
        if line.startswith(RELATION_SECTION_PREFIX) and line != RELATION_SECTION_HEADING
    ]
    if malformed:
        raise CleanupError("relation_section_malformed", "relation section is malformed")
    headings = [index for index, line in enumerate(lines) if line == RELATION_SECTION_HEADING]
    if not headings:
        raise CleanupError("relation_section_missing", "relation section is missing")
    if len(headings) != 1:
        raise CleanupError("relation_section_duplicate", "relation section is duplicated")
    start = headings[0] + 1
    end = len(lines)
    for index in range(start, len(lines)):
        if lines[index].startswith(b"## "):
            end = index
            break
    return list(range(start, end))


def splice_exact_relation_lines(data, expected_lines):
    """Remove each exact bytes line once and return unchanged-other-bytes postimage."""
    expected = list(expected_lines)
    if len(set(expected)) != len(expected):
        raise CleanupError("manifest_line_duplicate", "candidate lines are duplicated")
    for line in expected:
        if (
            not isinstance(line, bytes)
            or not line.endswith(b"\n")
            or b"\r" in line
            or line.count(b"\n") != 1
        ):
            raise CleanupError("relation_line_nonphysical", "candidate line is not exact LF")
    lines = data.splitlines(keepends=True)
    section_indexes = set(relation_section_line_indexes(data))
    remove_indexes = set()
    for expected_line in expected:
        inside = [
            index
            for index in section_indexes
            if lines[index] == expected_line
        ]
        outside = [
            index
            for index, line in enumerate(lines)
            if index not in section_indexes and line == expected_line
        ]
        if outside:
            raise CleanupError(
                "relation_line_outside_section", "candidate line also occurs outside section"
            )
        if not inside:
            raise CleanupError("relation_line_missing", "candidate line is missing")
        if len(inside) != 1:
            raise CleanupError("relation_line_duplicate", "candidate line is duplicated")
        remove_indexes.add(inside[0])
    return b"".join(
        line for index, line in enumerate(lines) if index not in remove_indexes
    )


def _renderer_for_subdir(obsidian_subdir):
    renderer = KnowledgeStore.__new__(KnowledgeStore)
    renderer.obsidian_subdir = obsidian_subdir
    return renderer


def _historical_source_material(backup_db, source_topic_id, obsidian_subdir):
    try:
        conn = open_read_only_db(backup_db, immutable=True)
    except sqlite3.Error as exc:
        raise CleanupError("backup_open", "verified backup could not be opened") from exc
    try:
        topic_row = conn.execute(
            "SELECT * FROM topics WHERE topic_id = ?", (source_topic_id,)
        ).fetchone()
        events = conn.execute(
            "SELECT * FROM events WHERE topic_id = ? ORDER BY created_at, event_id",
            (source_topic_id,),
        ).fetchall()
        relations = conn.execute(
            """
            SELECT r.relation, r.reason, r.target_topic_id, t.title, t.obsidian_path
            FROM relations r
            JOIN topics t ON t.topic_id = r.target_topic_id
            WHERE r.source_topic_id = ?
            ORDER BY r.created_at, r.relation
            """,
            (source_topic_id,),
        ).fetchall()
        if topic_row is None:
            raise CleanupError("topic_missing", "historical source topic is missing")
        topic_key = topic_row["topic_key"]
        title = topic_row["title"]
        if (
            (isinstance(topic_key, str) and topic_key.startswith("history-summary:"))
            or (isinstance(title, str) and "\u5386\u53f2\u603b\u7ed3" in title)
            or any(event["event_type"] == "history_summary" for event in events)
        ):
            raise CleanupError("history_topic", "historical summary topic is protected")
        renderer = _renderer_for_subdir(obsidian_subdir)
        topic = renderer._topic_dict(topic_row)
        rendered = renderer._render_markdown(topic, events, relations).encode("utf-8")
        legacy_rendered = renderer._render_markdown(
            topic, events, relations, include_source_contract=False
        ).encode("utf-8")
        return topic_row, (rendered, legacy_rendered)
    except CleanupError:
        raise
    except sqlite3.Error as exc:
        raise CleanupError("backup_schema", "historical renderer evidence failed") from exc
    except (KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise CleanupError("historical_renderer", "historical renderer refused input") from exc
    finally:
        conn.close()


def _current_rendered_lines(current_db, source_topic_id):
    try:
        conn = open_read_only_db(current_db)
    except sqlite3.Error as exc:
        raise CleanupError("current_open", "current database could not be opened") from exc
    try:
        rows = conn.execute(
            """
            SELECT r.relation, r.target_topic_id, t.title, t.obsidian_path
            FROM relations r
            JOIN topics t ON t.topic_id = r.target_topic_id
            WHERE r.source_topic_id = ?
            ORDER BY r.created_at, r.relation
            """,
            (source_topic_id,),
        ).fetchall()
        rendered = []
        for row in rows:
            relation = _require_text(
                row["relation"], code="current_value_type", field="relation"
            )
            title = _require_text(
                row["title"], code="current_value_type", field="title"
            )
            path = _require_text(
                row["obsidian_path"],
                code="current_value_type",
                field="obsidian_path",
            )
            target_id = _require_integer(
                row["target_topic_id"],
                code="current_value_type",
                field="target_topic_id",
            )
            if target_id == source_topic_id and relation in {
                "updates",
                "duplicate_of",
                "contradicts",
            }:
                continue
            rendered.append(
                (_render_relation_markdown_line(relation, path, title) + "\n").encode(
                    "utf-8"
                )
            )
        return rendered
    except CleanupError:
        raise
    except sqlite3.Error as exc:
        raise CleanupError("current_schema", "current relation rendering failed") from exc
    finally:
        conn.close()


def _atomic_private_json(directory_fd, filename, value):
    temporary = f".{filename}.{secrets.token_hex(16)}"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
        dir_fd=directory_fd,
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(canonical_json_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(
            temporary,
            filename,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _directory_entry_identity(parent_fd, name):
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return None
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        return None
    return _file_identity(info)


def _open_canonical_run_parent(parent_expression, expected_identity=None):
    if (
        not os.path.isabs(parent_expression)
        or os.path.realpath(parent_expression) != parent_expression
    ):
        raise CleanupError(
            "run_dir_parent_alias", "run directory parent must be canonical"
        )
    try:
        current_fd = os.open(os.path.sep, _directory_open_flags())
    except OSError as exc:
        raise CleanupError(
            "run_dir_parent_alias", "run directory parent is unavailable"
        ) from exc
    try:
        for component in [part for part in parent_expression.split(os.path.sep) if part]:
            try:
                next_fd = os.open(
                    component, _directory_open_flags(), dir_fd=current_fd
                )
            except OSError as exc:
                raise CleanupError(
                    "run_dir_parent_alias",
                    "run directory parent must have no symlink components",
                ) from exc
            os.close(current_fd)
            current_fd = next_fd
        identity = _file_identity(os.fstat(current_fd))
        try:
            path_info = os.stat(parent_expression, follow_symlinks=False)
        except OSError as exc:
            raise CleanupError(
                "run_dir_parent_alias", "run directory parent is unavailable"
            ) from exc
        if (
            not stat.S_ISDIR(path_info.st_mode)
            or stat.S_ISLNK(path_info.st_mode)
            or _file_identity(path_info) != identity
            or (expected_identity is not None and identity != expected_identity)
        ):
            raise CleanupError(
                "run_dir_parent_alias", "run directory parent identity changed"
            )
        return current_fd, identity
    except BaseException:
        os.close(current_fd)
        raise


def _revalidate_supplied_run_parent(parent_expression, expected_identity):
    descriptor, identity = _open_canonical_run_parent(
        parent_expression, expected_identity
    )
    os.close(descriptor)
    return identity


def _revalidate_supplied_run_target(
    parent_expression, final_name, parent_identity, final_identity
):
    descriptor, _identity = _open_canonical_run_parent(
        parent_expression, parent_identity
    )
    try:
        if _directory_entry_identity(descriptor, final_name) != final_identity:
            raise CleanupError(
                "run_dir_parent_alias", "supplied run directory identity changed"
            )
    finally:
        os.close(descriptor)


def _darwin_rename_exclusive_function():
    if sys.platform != "darwin":
        return None
    try:
        function = ctypes.CDLL(None, use_errno=True).renameatx_np
    except (AttributeError, OSError):
        return None
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    return function


def _exclusive_directory_publish_available():
    return _darwin_rename_exclusive_function() is not None


def _rename_directory_exclusive(parent_fd, source_name, destination_name):
    function = _darwin_rename_exclusive_function()
    if function is None:
        raise CleanupError(
            "run_dir_publish_unsupported",
            "exclusive directory publication is unavailable",
        )
    ctypes.set_errno(0)
    result = function(
        parent_fd,
        os.fsencode(source_name),
        parent_fd,
        os.fsencode(destination_name),
        0x00000004,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in (errno.EEXIST, errno.ENOTEMPTY):
        raise CleanupError(
            "run_dir_publish_conflict", "run directory publication conflicted"
        )
    raise CleanupError(
        "run_dir_publish_failed", "run directory publication failed"
    ) from OSError(error_number, os.strerror(error_number))


def _cleanup_staged_run(
    parent_fd,
    staging_fd,
    staging_name,
    final_name,
    identity,
    *,
    published,
):
    failures = []
    if identity is None:
        return "run_dir_cleanup"
    if staging_fd is not None:
        for filename in ("state.json", "manifest.json"):
            try:
                os.unlink(filename, dir_fd=staging_fd)
            except FileNotFoundError:
                pass
            except OSError as exc:
                failures.append(exc)
        try:
            os.fsync(staging_fd)
        except OSError as exc:
            failures.append(exc)
    reachable_name = final_name if published else staging_name
    if _directory_entry_identity(parent_fd, reachable_name) != identity:
        return "run_dir_substituted" if published else "run_dir_cleanup"
    try:
        os.rmdir(reachable_name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except OSError as exc:
        failures.append(exc)
    return "run_dir_cleanup" if failures else None


def _seal_run_artifacts(
    run_path,
    supplied_parent,
    supplied_parent_identity,
    manifest,
    initial_state,
):
    parent_path = supplied_parent
    final_name = os.path.basename(run_path)
    if not final_name or final_name in (".", ".."):
        raise CleanupError("run_dir_invalid", "run directory path is invalid")
    parent_fd, parent_identity = _open_canonical_run_parent(
        parent_path, supplied_parent_identity
    )
    staging_fd = None
    staging_name = None
    staging_identity = None
    published = False
    sealed = False
    error = None
    try:
        for _attempt in range(16):
            candidate_name = f".{final_name}.staging-{secrets.token_hex(16)}"
            try:
                os.mkdir(candidate_name, 0o700, dir_fd=parent_fd)
                staging_name = candidate_name
                break
            except FileExistsError:
                continue
        if staging_name is None:
            raise CleanupError("run_dir_staging", "private staging directory unavailable")
        created_identity = _directory_entry_identity(parent_fd, staging_name)
        if created_identity is None:
            raise CleanupError("run_dir_staging", "private staging directory changed")
        staging_identity = created_identity
        try:
            staging_fd = os.open(
                staging_name, _directory_open_flags(), dir_fd=parent_fd
            )
        except OSError as exc:
            raise CleanupError("run_dir_staging", "private staging directory changed") from exc
        opened_identity = _file_identity(os.fstat(staging_fd))
        if opened_identity != created_identity:
            raise CleanupError("run_dir_staging", "private staging directory changed")
        os.fchmod(staging_fd, 0o700)
        _atomic_private_json(staging_fd, "manifest.json", manifest)
        _atomic_private_json(staging_fd, "state.json", initial_state)
        if _directory_entry_identity(parent_fd, staging_name) != staging_identity:
            raise CleanupError("run_dir_staging", "private staging directory changed")
        _revalidate_supplied_run_parent(parent_path, parent_identity)
        _rename_directory_exclusive(parent_fd, staging_name, final_name)
        published = True
        os.fsync(parent_fd)
        if _directory_entry_identity(parent_fd, final_name) != staging_identity:
            raise CleanupError("run_dir_substituted", "published run directory changed")
        if _directory_entry_identity(parent_fd, staging_name) is not None:
            raise CleanupError("run_dir_publish_failed", "staging directory still exists")
        _revalidate_supplied_run_target(
            parent_path, final_name, parent_identity, staging_identity
        )
        sealed = True
    except CleanupError as exc:
        error = exc
    except (OSError, TypeError, ValueError) as exc:
        error = CleanupError("manifest_seal", "run artifacts could not be sealed")
        error.__cause__ = exc

    if not sealed:
        cleanup_code = _cleanup_staged_run(
            parent_fd,
            staging_fd,
            staging_name,
            final_name,
            staging_identity,
            published=published,
        )
        if staging_fd is not None:
            os.close(staging_fd)
            staging_fd = None
        os.close(parent_fd)
        if cleanup_code:
            raise CleanupError(cleanup_code, "run directory cleanup refused")
        raise error
    os.close(staging_fd)
    os.close(parent_fd)


def _read_private_json(directory_fd, filename):
    try:
        descriptor = os.open(filename, _candidate_open_flags(), dir_fd=directory_fd)
    except OSError as exc:
        raise CleanupError("sealed_run_missing", "sealed run artifact is missing") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise CleanupError("sealed_run_mode", "sealed run artifact is not private")
        data = _read_fd_bounded(descriptor)
    finally:
        os.close(descriptor)
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CleanupError("sealed_run_json", "sealed run artifact is invalid") from exc
    try:
        if data != canonical_json_bytes(value):
            raise CleanupError("sealed_run_canonical", "sealed run artifact is not canonical")
    except (TypeError, ValueError) as exc:
        raise CleanupError("sealed_run_canonical", "sealed run artifact is not canonical") from exc
    return value, data


def _require_sha256(value, *, code, field):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise CleanupError(code, f"{field} is not a SHA-256 digest")
    return value


def _validate_manifest_shape(manifest):
    code = "manifest_schema"
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        raise CleanupError(code, "manifest schema is invalid")
    inputs = manifest.get("inputs")
    if not isinstance(inputs, dict) or set(inputs) != {
        "backup_db",
        "current_db",
        "vault_root",
    }:
        raise CleanupError(code, "manifest inputs are invalid")
    for field, value in inputs.items():
        if (
            not isinstance(value, str)
            or not os.path.isabs(value)
            or os.path.realpath(value) != value
        ):
            raise CleanupError(code, f"manifest {field} path is not canonical")
    if sha256_bytes(inputs["backup_db"].encode("utf-8")) != manifest.get(
        "backup", {}
    ).get("path_sha256"):
        raise CleanupError(code, "manifest backup path binding is invalid")
    if sha256_bytes(inputs["current_db"].encode("utf-8")) != manifest.get(
        "current", {}
    ).get("path_sha256"):
        raise CleanupError(code, "manifest current path binding is invalid")
    if sha256_bytes(inputs["vault_root"].encode("utf-8")) != manifest.get(
        "vault", {}
    ).get("root_realpath_sha256"):
        raise CleanupError(code, "manifest vault path binding is invalid")
    _require_sha256(
        manifest.get("renderer_source_sha256"),
        code=code,
        field="renderer_source_sha256",
    )
    if manifest["renderer_source_sha256"] != sha256_bytes(
        read_file_bounded(knowledge_module.__file__)
    ):
        raise CleanupError("renderer_contract_drift", "renderer contract changed")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise CleanupError(code, "manifest files are invalid")
    seen_paths = set()
    for record in files:
        if not isinstance(record, dict):
            raise CleanupError(code, "manifest file record is invalid")
        relative_path = record.get("relative_path")
        _normalized_relative_path(relative_path, code=code)
        collision_key = unicodedata.normalize("NFC", relative_path).casefold()
        if collision_key in seen_paths:
            raise CleanupError(code, "manifest file paths collide")
        seen_paths.add(collision_key)
        for field in ("pre_sha256", "post_sha256"):
            _require_sha256(record.get(field), code=code, field=field)
        if type(record.get("mode")) is not int:
            raise CleanupError(code, "manifest file mode is invalid")
        if not isinstance(record.get("edges"), list) or not record["edges"]:
            raise CleanupError(code, "manifest file edges are invalid")
        for edge in record["edges"]:
            if not isinstance(edge, dict) or not isinstance(
                edge.get("rendered_line"), str
            ):
                raise CleanupError(code, "manifest edge is invalid")
    if manifest.get("counts", {}).get("unique_source_files") != len(files):
        raise CleanupError(code, "manifest file count is invalid")


def _validate_state_shape(state, manifest_sha256, manifest):
    if not isinstance(state, dict) or state.get("schema") != STATE_SCHEMA:
        raise CleanupError("state_schema", "state schema is invalid")
    if set(state) != {"schema", "manifest_sha256", "state", "files", "errors"}:
        raise CleanupError("state_schema", "state keys are invalid")
    if state.get("manifest_sha256") != manifest_sha256:
        raise CleanupError("state_manifest_digest", "state does not bind manifest")
    if state.get("state") not in {
        "planned",
        "backing_up",
        "backups_verified",
        "applying",
        "applied",
        "rolling_back",
        "rolled_back",
        "drifted",
    }:
        raise CleanupError("state_schema", "state phase is invalid")
    if not isinstance(state.get("files"), dict) or not isinstance(
        state.get("errors"), list
    ):
        raise CleanupError("state_schema", "state payload is invalid")
    if any(
        not isinstance(code, str)
        or not code
        or code[0] not in "abcdefghijklmnopqrstuvwxyz"
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in code)
        for code in state["errors"]
    ):
        raise CleanupError("state_schema", "state errors are invalid")
    records_by_path = {
        record["relative_path"]: record for record in manifest["files"]
    }
    if set(state["files"]) - set(records_by_path):
        raise CleanupError("state_schema", "state file path is invalid")
    for relative_path, file_state in state["files"].items():
        applied_evidence = (
            isinstance(file_state, dict)
            and set(file_state) == {"state", "post_sha256"}
            and file_state.get("state") == "applied"
            and file_state.get("post_sha256")
            == records_by_path[relative_path]["post_sha256"]
        )
        restored_evidence = (
            isinstance(file_state, dict)
            and set(file_state) == {"state", "pre_sha256"}
            and file_state.get("state") == "restored"
            and file_state.get("pre_sha256")
            == records_by_path[relative_path]["pre_sha256"]
        )
        if not (applied_evidence or restored_evidence):
            raise CleanupError("state_schema", "state file evidence is invalid")
    if state["state"] == "planned" and (state["files"] or state["errors"]):
        raise CleanupError("state_schema", "planned state must be empty")
    if state["state"] not in {"rolling_back", "rolled_back", "drifted"} and any(
        evidence.get("state") == "restored" for evidence in state["files"].values()
    ):
        raise CleanupError(
            "state_schema", "restored evidence is invalid outside rollback"
        )
    if state["state"] == "rolled_back" and (
        set(state["files"]) != set(records_by_path)
        or state["errors"]
        or any(
            evidence.get("state") != "restored"
            for evidence in state["files"].values()
        )
    ):
        raise CleanupError("state_schema", "rolled-back state is incomplete")


def _scan_private_artifact_tree(run_fd, tree):
    try:
        tree_fd = os.open(tree, _directory_open_flags(), dir_fd=run_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CleanupError("artifact_path", "artifact tree is unsafe") from exc
    files = set()
    directories = set()

    def _walk(directory_fd, prefix):
        try:
            names = os.listdir(directory_fd)
        except OSError as exc:
            raise CleanupError("artifact_path", "artifact tree is unreadable") from exc
        for name in names:
            if name in ("", ".", "..") or "/" in name:
                raise CleanupError("artifact_path", "artifact path is invalid")
            try:
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as exc:
                raise CleanupError("artifact_path", "artifact path changed") from exc
            relative = f"{prefix}/{name}" if prefix else name
            if stat.S_ISLNK(info.st_mode):
                raise CleanupError("artifact_path", "artifact path is symlinked")
            if stat.S_ISDIR(info.st_mode):
                if stat.S_IMODE(info.st_mode) != 0o700:
                    raise CleanupError(
                        "artifact_mode", "artifact directory is not private"
                    )
                try:
                    child_fd = os.open(
                        name, _directory_open_flags(), dir_fd=directory_fd
                    )
                except OSError as exc:
                    raise CleanupError("artifact_path", "artifact path changed") from exc
                try:
                    if _file_identity(os.fstat(child_fd)) != _file_identity(info):
                        raise CleanupError("artifact_path", "artifact path changed")
                    directories.add(relative)
                    _walk(child_fd, relative)
                finally:
                    os.close(child_fd)
                continue
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise CleanupError("artifact_mode", "artifact file is not private")
            files.add(relative)

    try:
        tree_info = os.fstat(tree_fd)
        if stat.S_IMODE(tree_info.st_mode) != 0o700:
            raise CleanupError("artifact_mode", "artifact tree is not private")
        _walk(tree_fd, "")
        return {"files": files, "directories": directories}
    finally:
        os.close(tree_fd)


def load_sealed_run(run_dir, *, _include_identity=False):
    """Return verified manifest, digest, and ledger; reject tampering."""
    run_path = os.path.abspath(os.path.expanduser(str(run_dir)))
    parent_path = os.path.dirname(run_path)
    final_name = os.path.basename(run_path)
    parent_fd, parent_identity = _open_canonical_run_parent(parent_path)
    run_fd = None
    try:
        try:
            run_fd = os.open(final_name, _directory_open_flags(), dir_fd=parent_fd)
        except OSError as exc:
            raise CleanupError("sealed_run_missing", "sealed run is missing") from exc
        run_info = os.fstat(run_fd)
        run_identity = _file_identity(run_info)
        if (
            stat.S_IMODE(run_info.st_mode) != 0o700
            or _directory_entry_identity(parent_fd, final_name) != run_identity
        ):
            raise CleanupError("sealed_run_mode", "sealed run is not private")
        manifest, manifest_bytes = _read_private_json(run_fd, "manifest.json")
        state, _state_bytes = _read_private_json(run_fd, "state.json")
        manifest_sha256 = sha256_bytes(manifest_bytes)
        _validate_manifest_shape(manifest)
        _validate_state_shape(state, manifest_sha256, manifest)
        expected_paths = {record["relative_path"] for record in manifest["files"]}
        expected_directories = set()
        for relative_path in expected_paths:
            parts = relative_path.split("/")
            expected_directories.update(
                "/".join(parts[:index]) for index in range(1, len(parts))
            )
        require_complete = state["state"] in {
            "backups_verified",
            "applying",
            "applied",
            "rolling_back",
            "rolled_back",
        }
        try:
            root_entries = set(os.listdir(run_fd))
        except OSError as exc:
            raise CleanupError("artifact_path", "sealed run root is unreadable") from exc
        base_entries = {"manifest.json", "state.json"}
        permitted_root_entries = base_entries | {"backups", "staged"}
        if root_entries - permitted_root_entries:
            raise CleanupError(
                "artifact_unexpected", "sealed run root has unexpected entries"
            )
        if state["state"] == "planned" and root_entries != base_entries:
            raise CleanupError(
                "artifacts_before_backup", "unexpected artifacts exist before backup"
            )
        if require_complete and root_entries != permitted_root_entries:
            raise CleanupError(
                "artifact_missing", "verified artifact trees are incomplete"
            )
        for name in ("backups", "staged"):
            tree_entries = _scan_private_artifact_tree(run_fd, name)
            if state["state"] == "planned":
                if tree_entries is not None:
                    raise CleanupError(
                        "artifacts_before_backup",
                        "unexpected artifacts exist before backup",
                    )
                continue
            tree_entries = tree_entries or {"files": set(), "directories": set()}
            actual_paths = tree_entries["files"]
            actual_directories = tree_entries["directories"]
            if actual_paths - expected_paths:
                raise CleanupError(
                    "artifact_unexpected", "artifact tree has unexpected paths"
                )
            if require_complete and actual_paths != expected_paths:
                raise CleanupError(
                    "artifact_missing", "verified artifact tree is incomplete"
                )
            if actual_directories - expected_directories:
                raise CleanupError(
                    "artifact_unexpected", "artifact tree has unexpected directories"
                )
            empty_directories = {
                directory
                for directory in actual_directories
                if not any(
                    path.startswith(f"{directory}/") for path in actual_paths
                )
            }
            if empty_directories:
                raise CleanupError(
                    "artifact_unexpected", "artifact tree has empty directories"
                )
            if require_complete and actual_directories != expected_directories:
                raise CleanupError(
                    "artifact_missing", "verified artifact directories are incomplete"
                )
        _revalidate_supplied_run_target(
            parent_path, final_name, parent_identity, run_identity
        )
        result = (manifest, manifest_sha256, state)
        if _include_identity:
            return (*result, run_identity)
        return result
    finally:
        if run_fd is not None:
            os.close(run_fd)
        os.close(parent_fd)


def atomic_write_private(
    path,
    data,
    mode=0o600,
    *,
    _expected_parent_identity=None,
    _expected_destination_identity=None,
    _expected_destination_sha256=None,
    _expected_destination_mode=None,
    _identity_error_code="run_dir_identity",
    _exclusive_create=False,
):
    """Write bytes, fsync, chmod, replace, fsync parent, and read-back verify."""
    if not isinstance(data, bytes):
        raise CleanupError("private_write_type", "private write payload must be bytes")
    destination = os.path.abspath(os.path.expanduser(str(path)))
    parent_path = os.path.dirname(destination)
    filename = os.path.basename(destination)
    if not filename or filename in (".", ".."):
        raise CleanupError("private_write_path", "private write path is invalid")
    parent_fd, parent_identity = _open_canonical_run_parent(parent_path)
    if (
        _expected_parent_identity is not None
        and parent_identity != _expected_parent_identity
    ):
        os.close(parent_fd)
        raise CleanupError(_identity_error_code, "private write parent identity changed")
    temporary = f".{filename}.{secrets.token_hex(16)}"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            mode,
            dir_fd=parent_fd,
        )
        os.fchmod(descriptor, mode)
        offset = 0
        while offset < len(data):
            offset += os.write(descriptor, data[offset:])
        os.fsync(descriptor)
        if _read_fd_bounded(descriptor) != data:
            raise CleanupError("private_write_verify", "private write verification failed")
        os.close(descriptor)
        descriptor = -1
        _revalidate_supplied_run_parent(parent_path, parent_identity)
        if _expected_destination_identity is not None:
            try:
                destination_info = os.stat(
                    filename, dir_fd=parent_fd, follow_symlinks=False
                )
            except OSError as exc:
                raise CleanupError(
                    _identity_error_code,
                    "private write destination identity changed",
                ) from exc
            if (
                stat.S_ISLNK(destination_info.st_mode)
                or not stat.S_ISREG(destination_info.st_mode)
                or _file_identity(destination_info)
                != _expected_destination_identity
            ):
                raise CleanupError(
                    _identity_error_code,
                    "private write destination identity changed",
                )
            destination_fd = -1
            try:
                try:
                    destination_fd = os.open(
                        filename, _candidate_open_flags(), dir_fd=parent_fd
                    )
                except OSError as exc:
                    raise CleanupError(
                        _identity_error_code,
                        "private write destination identity changed",
                    ) from exc
                current_info = os.fstat(destination_fd)
                if (
                    _file_identity(current_info) != _expected_destination_identity
                    or not stat.S_ISREG(current_info.st_mode)
                    or current_info.st_nlink != 1
                ):
                    raise CleanupError(
                        _identity_error_code,
                        "private write destination identity changed",
                    )
                if (
                    _expected_destination_mode is not None
                    and stat.S_IMODE(current_info.st_mode)
                    != _expected_destination_mode
                ):
                    raise CleanupError(
                        "destination_drift", "destination mode changed before replace"
                    )
                current_data = _read_fd_bounded(destination_fd)
                if (
                    _expected_destination_sha256 is not None
                    and sha256_bytes(current_data)
                    != _expected_destination_sha256
                ):
                    raise CleanupError(
                        "destination_drift", "destination bytes changed before replace"
                    )
            finally:
                if destination_fd >= 0:
                    os.close(destination_fd)
        if _exclusive_create:
            try:
                _rename_directory_exclusive(parent_fd, temporary, filename)
            except CleanupError as exc:
                if exc.code == "run_dir_publish_conflict":
                    raise CleanupError(
                        "artifact_conflict", "artifact leaf already exists"
                    ) from exc
                if exc.code == "run_dir_publish_unsupported":
                    raise CleanupError(
                        "artifact_publish_unsupported",
                        "exclusive artifact publication is unavailable",
                    ) from exc
                raise CleanupError(
                    "artifact_publish_failed", "exclusive artifact publication failed"
                ) from exc
        else:
            os.replace(
                temporary,
                filename,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        os.fsync(parent_fd)
        check_fd = os.open(filename, _candidate_open_flags(), dir_fd=parent_fd)
        try:
            info = os.fstat(check_fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != mode
                or _read_fd_bounded(check_fd) != data
            ):
                raise CleanupError(
                    "private_write_verify", "private write verification failed"
                )
        finally:
            os.close(check_fd)
        _revalidate_supplied_run_parent(parent_path, parent_identity)
        return sha256_bytes(data)
    except CleanupError:
        raise
    except OSError as exc:
        raise CleanupError("private_write_failed", "private write failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        finally:
            os.close(parent_fd)


def _require_run_identity(run_dir, expected_identity):
    run_path = os.path.abspath(os.path.expanduser(str(run_dir)))
    parent_path = os.path.dirname(run_path)
    final_name = os.path.basename(run_path)
    parent_fd, _parent_identity = _open_canonical_run_parent(parent_path)
    try:
        try:
            run_fd = os.open(final_name, _directory_open_flags(), dir_fd=parent_fd)
        except OSError as exc:
            raise CleanupError("run_dir_identity", "sealed run identity changed") from exc
        try:
            info = os.fstat(run_fd)
            if (
                _file_identity(info) != expected_identity
                or stat.S_IMODE(info.st_mode) != 0o700
                or _directory_entry_identity(parent_fd, final_name)
                != expected_identity
            ):
                raise CleanupError("run_dir_identity", "sealed run identity changed")
        finally:
            os.close(run_fd)
    finally:
        os.close(parent_fd)


def _ensure_private_artifact_parent(
    run_dir, tree, relative_path, run_identity
):
    parts = _normalized_relative_path(relative_path, code="artifact_path")
    run_path = os.path.abspath(os.path.expanduser(str(run_dir)))
    run_fd = os.open(run_path, _directory_open_flags())
    current_fd = run_fd
    created_directories = []
    current_parts = []
    try:
        if (
            _file_identity(os.fstat(run_fd)) != run_identity
            or stat.S_IMODE(os.fstat(run_fd).st_mode) != 0o700
        ):
            raise CleanupError("run_dir_identity", "sealed run identity changed")
        for part in [tree, *parts[:-1]]:
            current_parts.append(part)
            created = False
            try:
                os.mkdir(part, 0o700, dir_fd=current_fd)
                created = True
            except FileExistsError:
                pass
            try:
                next_fd = os.open(part, _directory_open_flags(), dir_fd=current_fd)
            except OSError as exc:
                raise CleanupError(
                    "artifact_path", "artifact directory is unsafe"
                ) from exc
            info = os.fstat(next_fd)
            if stat.S_IMODE(info.st_mode) != 0o700:
                os.close(next_fd)
                raise CleanupError("artifact_mode", "artifact directory is not private")
            if created:
                created_directories.append(
                    (
                        os.path.join(run_path, *current_parts),
                        _file_identity(info),
                    )
                )
            if current_fd != run_fd:
                os.close(current_fd)
            current_fd = next_fd
        return (
            os.path.join(run_path, tree, *parts),
            _file_identity(os.fstat(current_fd)),
            created_directories,
        )
    finally:
        if current_fd != run_fd:
            os.close(current_fd)
        os.close(run_fd)


def _remove_owned_empty_artifact_directories(
    run_dir, created_directories, run_identity
):
    for directory_path, expected_identity in reversed(created_directories):
        _require_run_identity(run_dir, run_identity)
        parent_path = os.path.dirname(directory_path)
        name = os.path.basename(directory_path)
        try:
            parent_fd, _parent_identity = _open_canonical_run_parent(parent_path)
        except CleanupError:
            continue
        try:
            try:
                info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except OSError:
                continue
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISDIR(info.st_mode)
                or _file_identity(info) != expected_identity
            ):
                continue
            try:
                os.rmdir(name, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except OSError as exc:
                if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                    raise CleanupError(
                        "artifact_cleanup", "owned artifact directory cleanup failed"
                    ) from exc
        finally:
            os.close(parent_fd)


def _read_private_artifact(
    path, expected_sha256, *, run_dir=None, run_identity=None
):
    if run_dir is not None and run_identity is not None:
        _require_run_identity(run_dir, run_identity)
    try:
        info = os.lstat(path)
    except FileNotFoundError as exc:
        raise CleanupError("artifact_missing", "verified artifact is missing") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise CleanupError("artifact_mode", "verified artifact is unsafe")
    absolute = os.path.abspath(os.path.expanduser(str(path)))
    parent_path = os.path.dirname(absolute)
    filename = os.path.basename(absolute)
    parent_fd, parent_identity = _open_canonical_run_parent(parent_path)
    descriptor = -1
    try:
        try:
            descriptor = os.open(
                filename, _candidate_open_flags(), dir_fd=parent_fd
            )
        except OSError as exc:
            raise CleanupError("artifact_path", "verified artifact path changed") from exc
        opened_info = os.fstat(descriptor)
        if (
            _file_identity(opened_info) != _file_identity(info)
            or not stat.S_ISREG(opened_info.st_mode)
            or opened_info.st_nlink != 1
            or stat.S_IMODE(opened_info.st_mode) != 0o600
        ):
            raise CleanupError("artifact_path", "verified artifact path changed")
        data = _read_fd_bounded(descriptor)
        _revalidate_supplied_run_parent(parent_path, parent_identity)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)
    if sha256_bytes(data) != expected_sha256:
        raise CleanupError("artifact_checksum", "verified artifact checksum mismatch")
    return data


def _write_state(run_dir, state, run_identity):
    atomic_write_private(
        os.path.join(run_dir, "state.json"),
        canonical_json_bytes(state),
        0o600,
        _expected_parent_identity=run_identity,
    )


def _write_drifted_state(run_dir, state, run_identity, code):
    state["state"] = "drifted"
    if code not in state["errors"]:
        state["errors"].append(code)
    _write_state(run_dir, state, run_identity)


def _revalidate_database_evidence(manifest):
    counts = manifest["counts"]
    evidence = collect_database_evidence(
        manifest["inputs"]["backup_db"],
        manifest["inputs"]["current_db"],
        CleanupExpectations(
            backup_sha256=manifest["backup"]["sha256"],
            selected_edges=counts["selected"],
            self_loops=counts["self_loops"],
            renderable_edges=counts["renderable"],
        ),
    )
    comparisons = {
        "backup_sha256": (evidence["backup_sha256"], manifest["backup"]["sha256"]),
        "backup_selected_digest": (
            evidence["selected_edge_digest"],
            manifest["backup"]["selected_edge_set_digest"],
        ),
        "current_snapshot_digest": (
            evidence["current_snapshot_digest"],
            manifest["current"]["snapshot_digest"],
        ),
        "current_relation_count": (
            evidence["current_relation_count"],
            manifest["current"]["relation_count"],
        ),
        "current_relation_digest": (
            evidence["current_relation_set_digest"],
            manifest["current"]["relation_set_digest"],
        ),
        "current_warning_count": (
            evidence["risky_warning_count"],
            manifest["current"]["risky_warning_count"],
        ),
        "current_warning_digest": (
            evidence["risky_warning_set_digest"],
            manifest["current"]["risky_warning_set_digest"],
        ),
    }
    if any(observed != expected for observed, expected in comparisons.values()):
        raise CleanupError("database_snapshot_drift", "database evidence drifted")
    return evidence


def _preflight_destination(manifest, record):
    data, info, identity_chain = _read_candidate_from_pinned_root(
        manifest["inputs"]["vault_root"],
        manifest["vault"]["obsidian_subdir"],
        record["relative_path"],
        _include_identity_chain=True,
    )
    if stat.S_IMODE(info.st_mode) != record["mode"]:
        raise CleanupError("destination_mode_drift", "destination mode drifted")
    return data, sha256_bytes(data), identity_chain


def apply_cleanup(run_dir, manifest_sha256, confirm, *, fault_hook=None):
    """Apply a sealed exact-splice plan with verified backup and resumable state."""
    manifest, sealed_sha256, state, run_identity = load_sealed_run(
        run_dir, _include_identity=True
    )
    if manifest_sha256 != sealed_sha256:
        raise CleanupError("manifest_digest", "manifest digest does not match")
    if confirm != f"{APPLY_TOKEN_PREFIX}{sealed_sha256}":
        raise CleanupError("apply_confirmation", "apply confirmation does not match")
    if state["state"] == "drifted":
        raise CleanupError("destination_drift", "sealed run is drifted")
    if state["state"] in {"rolling_back", "rolled_back"}:
        raise CleanupError("apply_state", "rolled-back run cannot be applied")

    _revalidate_database_evidence(manifest)
    entry_phase = state["state"]
    verified_phases = {"backups_verified", "applying", "applied"}
    observed_preimages = {}
    verified_backups = {}
    verified_staged = {}
    for record in manifest["files"]:
        relative_path = record["relative_path"]
        backup_path = os.path.join(run_dir, "backups", relative_path)
        staged_path = os.path.join(run_dir, "staged", relative_path)
        backup_data = None
        staged_data = None
        if os.path.lexists(backup_path):
            backup_data = _read_private_artifact(
                backup_path,
                record["pre_sha256"],
                run_dir=run_dir,
                run_identity=run_identity,
            )
            verified_backups[relative_path] = backup_data
        if os.path.lexists(staged_path):
            staged_data = _read_private_artifact(
                staged_path,
                record["post_sha256"],
                run_dir=run_dir,
                run_identity=run_identity,
            )
            verified_staged[relative_path] = staged_data
        data, digest, _identity_chain = _preflight_destination(manifest, record)
        if digest == record["pre_sha256"]:
            observed_preimages[relative_path] = data
        elif digest == record["post_sha256"]:
            if entry_phase not in verified_phases or backup_data is None:
                raise CleanupError(
                    "postimage_before_backups_verified",
                    "postimage exists before verified backup state",
                )
        else:
            state["state"] = "drifted"
            state["errors"].append("destination_drift")
            _write_state(run_dir, state, run_identity)
            raise CleanupError(
                "destination_drift", "destination is neither preimage nor postimage"
            )

    if entry_phase in verified_phases:
        for record in manifest["files"]:
            relative_path = record["relative_path"]
            if (
                relative_path not in verified_backups
                or relative_path not in verified_staged
            ):
                raise CleanupError(
                    "artifact_missing", "verified backup or staged artifact is missing"
                )
    else:
        state["state"] = "backing_up"
        _write_state(run_dir, state, run_identity)
    for record in manifest["files"]:
        relative_path = record["relative_path"]
        preimage = observed_preimages.get(relative_path)
        if preimage is None:
            preimage = verified_backups.get(relative_path)
        if preimage is None:
            raise CleanupError("artifact_missing", "preimage evidence is missing")
        postimage = splice_exact_relation_lines(
            preimage,
            [edge["rendered_line"].encode("utf-8") for edge in record["edges"]],
        )
        if sha256_bytes(postimage) != record["post_sha256"]:
            raise CleanupError("postimage_drift", "postimage does not match manifest")
        if relative_path not in verified_backups:
            (
                backup_path,
                backup_parent_identity,
                backup_created_directories,
            ) = _ensure_private_artifact_parent(
                run_dir, "backups", relative_path, run_identity
            )
            try:
                atomic_write_private(
                    backup_path,
                    preimage,
                    0o600,
                    _expected_parent_identity=backup_parent_identity,
                    _identity_error_code="artifact_path",
                    _exclusive_create=True,
                )
            except BaseException:
                _remove_owned_empty_artifact_directories(
                    run_dir, backup_created_directories, run_identity
                )
                raise
            verified_backups[relative_path] = _read_private_artifact(
                backup_path,
                record["pre_sha256"],
                run_dir=run_dir,
                run_identity=run_identity,
            )
        if relative_path not in verified_staged:
            (
                staged_path,
                staged_parent_identity,
                staged_created_directories,
            ) = _ensure_private_artifact_parent(
                run_dir, "staged", relative_path, run_identity
            )
            try:
                atomic_write_private(
                    staged_path,
                    postimage,
                    0o600,
                    _expected_parent_identity=staged_parent_identity,
                    _identity_error_code="artifact_path",
                    _exclusive_create=True,
                )
            except BaseException:
                _remove_owned_empty_artifact_directories(
                    run_dir, staged_created_directories, run_identity
                )
                raise
            verified_staged[relative_path] = _read_private_artifact(
                staged_path,
                record["post_sha256"],
                run_dir=run_dir,
                run_identity=run_identity,
            )

    for record in manifest["files"]:
        relative_path = record["relative_path"]
        _read_private_artifact(
            os.path.join(run_dir, "backups", relative_path),
            record["pre_sha256"],
            run_dir=run_dir,
            run_identity=run_identity,
        )
        _read_private_artifact(
            os.path.join(run_dir, "staged", relative_path),
            record["post_sha256"],
            run_dir=run_dir,
            run_identity=run_identity,
        )
    if entry_phase not in verified_phases:
        state["state"] = "backups_verified"
        _write_state(run_dir, state, run_identity)

    _revalidate_database_evidence(manifest)
    state["state"] = "applying"
    _write_state(run_dir, state, run_identity)
    for record in manifest["files"]:
        relative_path = record["relative_path"]
        _barrier_data, barrier_digest, _barrier_identity = _preflight_destination(
            manifest, record
        )
        if barrier_digest == record["pre_sha256"]:
            continue
        if barrier_digest == record["post_sha256"]:
            if relative_path not in verified_backups:
                raise CleanupError(
                    "artifact_missing", "postimage has no verified preimage backup"
                )
            continue
        state["state"] = "drifted"
        state["errors"].append("destination_drift")
        _write_state(run_dir, state, run_identity)
        raise CleanupError(
            "destination_drift", "destination failed all-file apply barrier"
        )
    applied = 0
    already_clean = 0
    for record in manifest["files"]:
        relative_path = record["relative_path"]
        _data, digest, expected_identity_chain = _preflight_destination(
            manifest, record
        )
        if digest == record["post_sha256"]:
            already_clean += 1
            continue
        if digest != record["pre_sha256"]:
            state["state"] = "drifted"
            state["errors"].append("destination_drift")
            _write_state(run_dir, state, run_identity)
            raise CleanupError(
                "destination_drift", "destination is neither preimage nor postimage"
            )
        staged = _read_private_artifact(
            os.path.join(run_dir, "staged", relative_path),
            record["post_sha256"],
            run_dir=run_dir,
            run_identity=run_identity,
        )
        if fault_hook is not None:
            fault_hook("before_replace", relative_path)
        _latest_data, latest_digest, latest_identity_chain = _preflight_destination(
            manifest, record
        )
        if latest_identity_chain != expected_identity_chain:
            _write_drifted_state(
                run_dir, state, run_identity, "topic_identity_race"
            )
            raise CleanupError("topic_identity_race", "topic path identity changed")
        if latest_digest != record["pre_sha256"]:
            state["state"] = "drifted"
            state["errors"].append("destination_drift")
            _write_state(run_dir, state, run_identity)
            raise CleanupError(
                "destination_drift", "destination changed before replacement"
            )
        destination = os.path.join(
            manifest["inputs"]["vault_root"], *relative_path.split("/")
        )
        try:
            atomic_write_private(
                destination,
                staged,
                record["mode"],
                _expected_parent_identity=latest_identity_chain[-2],
                _expected_destination_identity=latest_identity_chain[-1],
                _expected_destination_sha256=record["pre_sha256"],
                _expected_destination_mode=record["mode"],
                _identity_error_code="topic_identity_race",
            )
        except CleanupError as exc:
            if exc.code in {"destination_drift", "topic_identity_race"}:
                _write_drifted_state(
                    run_dir, state, run_identity, exc.code
                )
            raise
        if fault_hook is not None:
            fault_hook("after_replace", relative_path)
        state["files"][relative_path] = {
            "state": "applied",
            "post_sha256": record["post_sha256"],
        }
        _write_state(run_dir, state, run_identity)
        applied += 1
        if fault_hook is not None:
            fault_hook("after_ledger", relative_path)

    _revalidate_database_evidence(manifest)
    _verified_markdown_backups(
        run_dir, manifest, run_identity, require_all=True
    )
    has_pending = False
    for record in manifest["files"]:
        try:
            _data, digest, _identity_chain = _preflight_destination(
                manifest, record
            )
        except CleanupError as exc:
            _write_drifted_state(run_dir, state, run_identity, exc.code)
            raise
        if digest == record["post_sha256"]:
            continue
        if digest == record["pre_sha256"]:
            has_pending = True
            continue
        _write_drifted_state(
            run_dir, state, run_identity, "destination_drift"
        )
        raise CleanupError(
            "destination_drift",
            "destination failed final all-postimage barrier",
        )
    if has_pending:
        raise CleanupError(
            "apply_incomplete",
            "known preimage remains after final apply barrier",
        )
    state["state"] = "applied"
    _write_state(run_dir, state, run_identity)
    return {
        "state": "applied",
        "manifest_sha256": sealed_sha256,
        "applied_this_invocation": applied,
        "already_clean": already_clean,
        "pending": 0,
        "drifted": 0,
    }


def _verified_markdown_backups(
    run_dir, manifest, run_identity, *, require_all
):
    verified = {}
    for record in manifest["files"]:
        relative_path = record["relative_path"]
        backup_path = os.path.join(run_dir, "backups", relative_path)
        if not os.path.lexists(backup_path):
            if require_all:
                raise CleanupError("artifact_missing", "verified backup is missing")
            continue
        verified[relative_path] = _read_private_artifact(
            backup_path,
            record["pre_sha256"],
            run_dir=run_dir,
            run_identity=run_identity,
        )
    return verified


def status_cleanup(run_dir):
    """Derive sealed-run state from read-only database, artifact, and disk evidence."""
    manifest, sealed_sha256, state, run_identity = load_sealed_run(
        run_dir, _include_identity=True
    )
    evidence = _revalidate_database_evidence(manifest)
    verified_phases = {
        "backups_verified",
        "applying",
        "applied",
        "rolling_back",
        "rolled_back",
    }
    backups = _verified_markdown_backups(
        run_dir,
        manifest,
        run_identity,
        require_all=state["state"] in verified_phases,
    )
    preimage_count = 0
    postimage_count = 0
    drifted = 0
    for record in manifest["files"]:
        relative_path = record["relative_path"]
        _data, digest, _identity_chain = _preflight_destination(manifest, record)
        if digest == record["pre_sha256"]:
            preimage_count += 1
        elif digest == record["post_sha256"]:
            if relative_path not in backups:
                raise CleanupError(
                    "postimage_before_backups_verified",
                    "postimage has no verified preimage backup",
                )
            postimage_count += 1
        else:
            drifted += 1
    return {
        "state": state["state"],
        "manifest_sha256": sealed_sha256,
        "preimage_count": preimage_count,
        "postimage_count": postimage_count,
        "pending": preimage_count,
        "already_clean": postimage_count,
        "drifted": drifted,
        "current_relation_count": evidence["current_relation_count"],
        "current_relation_set_digest": evidence["current_relation_set_digest"],
        "risky_warning_count": evidence["risky_warning_count"],
        "risky_warning_set_digest": evidence["risky_warning_set_digest"],
        "current_snapshot_digest": evidence["current_snapshot_digest"],
    }


def rollback_cleanup(
    run_dir,
    manifest_sha256,
    confirm,
    *,
    fault_hook=None,
):
    """Restore verified Markdown preimages with all-file preflight and resume."""
    manifest, sealed_sha256, state, run_identity = load_sealed_run(
        run_dir, _include_identity=True
    )
    if manifest_sha256 != sealed_sha256:
        raise CleanupError("manifest_digest", "manifest digest does not match")
    if confirm != f"{ROLLBACK_TOKEN_PREFIX}{sealed_sha256}":
        raise CleanupError(
            "rollback_confirmation", "rollback confirmation does not match"
        )
    if state["state"] not in {
        "applying",
        "applied",
        "rolling_back",
        "rolled_back",
    }:
        raise CleanupError("rollback_state", "sealed run cannot be rolled back")

    _revalidate_database_evidence(manifest)
    verified_backups = _verified_markdown_backups(
        run_dir, manifest, run_identity, require_all=True
    )
    initial_classification = {}
    for record in manifest["files"]:
        relative_path = record["relative_path"]
        _data, digest, identity_chain = _preflight_destination(manifest, record)
        if digest == record["pre_sha256"]:
            classification = "pre"
        elif digest == record["post_sha256"]:
            classification = "post"
        else:
            raise CleanupError(
                "destination_drift",
                "rollback destination is neither preimage nor postimage",
            )
        initial_classification[relative_path] = (classification, identity_chain)

    already_restored = sum(
        classification == "pre"
        for classification, _identity in initial_classification.values()
    )
    if state["state"] == "rolled_back" and already_restored == len(manifest["files"]):
        _revalidate_database_evidence(manifest)
        return {
            "state": "rolled_back",
            "manifest_sha256": sealed_sha256,
            "restored": 0,
            "already_restored": already_restored,
            "drifted": 0,
        }

    state["state"] = "rolling_back"
    _write_state(run_dir, state, run_identity)
    verified_backups = _verified_markdown_backups(
        run_dir, manifest, run_identity, require_all=True
    )
    rollback_barrier = {}
    for record in manifest["files"]:
        relative_path = record["relative_path"]
        _data, digest, identity_chain = _preflight_destination(manifest, record)
        if digest == record["pre_sha256"]:
            classification = "pre"
        elif digest == record["post_sha256"]:
            classification = "post"
        else:
            raise CleanupError(
                "destination_drift",
                "rollback destination failed post-ledger all-file barrier",
            )
        rollback_barrier[relative_path] = (classification, identity_chain)
    already_restored = sum(
        classification == "pre"
        for classification, _identity in rollback_barrier.values()
    )
    restored = 0
    for record in manifest["files"]:
        relative_path = record["relative_path"]
        _classification, expected_identity_chain = rollback_barrier[relative_path]
        _data, digest, current_identity_chain = _preflight_destination(manifest, record)
        if current_identity_chain != expected_identity_chain:
            raise CleanupError("topic_identity_race", "rollback destination identity changed")
        if digest == record["pre_sha256"]:
            evidence = {
                "state": "restored",
                "pre_sha256": record["pre_sha256"],
            }
            if state["files"].get(relative_path) != evidence:
                state["files"][relative_path] = evidence
                _write_state(run_dir, state, run_identity)
            continue
        if digest != record["post_sha256"]:
            raise CleanupError(
                "destination_drift", "rollback destination changed after preflight"
            )
        if fault_hook is not None:
            fault_hook("before_restore", relative_path)
        _latest_data, latest_digest, latest_identity_chain = _preflight_destination(
            manifest, record
        )
        if latest_identity_chain != current_identity_chain:
            raise CleanupError("topic_identity_race", "rollback destination identity changed")
        if latest_digest != record["post_sha256"]:
            raise CleanupError(
                "destination_drift", "rollback destination changed before replacement"
            )
        destination = os.path.join(
            manifest["inputs"]["vault_root"], *relative_path.split("/")
        )
        atomic_write_private(
            destination,
            verified_backups[relative_path],
            record["mode"],
            _expected_parent_identity=latest_identity_chain[-2],
            _expected_destination_identity=latest_identity_chain[-1],
            _expected_destination_sha256=record["post_sha256"],
            _expected_destination_mode=record["mode"],
            _identity_error_code="topic_identity_race",
        )
        if fault_hook is not None:
            fault_hook("after_restore", relative_path)
        _restored_data, restored_digest, _restored_identity = _preflight_destination(
            manifest, record
        )
        if restored_digest != record["pre_sha256"]:
            raise CleanupError("rollback_verify", "restored preimage verification failed")
        state["files"][relative_path] = {
            "state": "restored",
            "pre_sha256": record["pre_sha256"],
        }
        _write_state(run_dir, state, run_identity)
        restored += 1
        if fault_hook is not None:
            fault_hook("after_ledger", relative_path)

    _revalidate_database_evidence(manifest)
    for record in manifest["files"]:
        _data, digest, _identity_chain = _preflight_destination(manifest, record)
        if digest != record["pre_sha256"]:
            raise CleanupError(
                "rollback_verify", "not every destination is restored"
            )
        state["files"][record["relative_path"]] = {
            "state": "restored",
            "pre_sha256": record["pre_sha256"],
        }
    state["state"] = "rolled_back"
    state["errors"] = []
    _write_state(run_dir, state, run_identity)
    return {
        "state": "rolled_back",
        "manifest_sha256": sealed_sha256,
        "restored": restored,
        "already_restored": already_restored,
        "drifted": 0,
    }


def preview_cleanup(
    *,
    backup_db,
    current_db,
    vault_root,
    obsidian_subdir,
    run_dir,
    generator_commit,
    expectations=LIVE_EXPECTATIONS,
):
    """Validate every candidate and seal an immutable exact-splice plan."""
    if not isinstance(generator_commit, str) or not generator_commit.strip():
        raise CleanupError("generator_commit_blank", "generator commit is required")
    run_path = os.path.abspath(os.path.expanduser(str(run_dir)))
    supplied_run_parent = os.path.dirname(run_path)
    parent_fd, supplied_parent_identity = _open_canonical_run_parent(
        supplied_run_parent
    )
    os.close(parent_fd)
    if os.path.lexists(run_path):
        raise CleanupError("run_dir_exists", "run directory already exists")
    subdir_parts = _normalized_relative_path(
        str(obsidian_subdir), code="obsidian_subdir_invalid"
    )
    normalized_subdir = "/".join(subdir_parts)
    evidence = collect_database_evidence(backup_db, current_db, expectations)

    grouped = {}
    collision_keys = {}
    for edge in evidence["renderable_edges"]:
        title = edge["target_title"]
        if "\n" in title or "\r" in title:
            raise CleanupError("multiline_target_title", "target title spans lines")
        line = edge["rendered_line"].encode("utf-8")
        if line.count(b"\n") != 1 or not line.endswith(b"\n") or b"\r" in line:
            raise CleanupError("relation_line_nonphysical", "rendered line is not exact LF")
        path = edge["source_path"]
        collision_key = unicodedata.normalize("NFC", path).casefold()
        previous = collision_keys.get(collision_key)
        if previous is not None and previous != path:
            raise CleanupError("manifest_path_collision", "manifest paths collide")
        collision_keys[collision_key] = path
        grouped.setdefault(path, []).append((edge, line))

    file_records = []
    current_identity_by_id = {
        identity[0]: identity for identity in evidence["source_identities"]
    }
    exact_match_count = 0
    total_preimage_bytes = 0
    total_postimage_bytes = 0
    root_realpath = os.path.realpath(os.path.abspath(os.path.expanduser(str(vault_root))))
    backup_realpath = os.path.realpath(
        os.path.abspath(os.path.expanduser(str(backup_db)))
    )
    current_realpath = os.path.realpath(
        os.path.abspath(os.path.expanduser(str(current_db)))
    )
    for source_path in sorted(grouped):
        edge_lines = grouped[source_path]
        source_topic_id = edge_lines[0][0]["source_topic_id"]
        if any(edge["source_topic_id"] != source_topic_id for edge, _ in edge_lines):
            raise CleanupError("source_path_owner_count", "source path has multiple topics")
        topic_row, historical_preimages = _historical_source_material(
            backup_db, source_topic_id, normalized_subdir
        )
        observed_preimage, info = _read_candidate_from_pinned_root(
            root_realpath, normalized_subdir, source_path
        )
        matching_preimages = [
            preimage
            for preimage in historical_preimages
            if observed_preimage == preimage
        ]
        if not matching_preimages:
            raise CleanupError("full_preimage_drift", "historical preimage does not match")
        historical_preimage = matching_preimages[0]
        expected_lines = [line for _, line in edge_lines]
        current_lines = _current_rendered_lines(current_db, source_topic_id)
        if any(line in current_lines for line in expected_lines):
            raise CleanupError(
                "current_relation_collision", "current relation protects candidate line"
            )
        postimage = splice_exact_relation_lines(observed_preimage, expected_lines)
        pre_sha256 = sha256_bytes(observed_preimage)
        post_sha256 = sha256_bytes(postimage)
        edges = []
        for edge, line in edge_lines:
            edges.append(
                {
                    "relation_id": edge["relation_id"],
                    "source_topic_id": edge["source_topic_id"],
                    "target_topic_id": edge["target_topic_id"],
                    "source_path": edge["source_path"],
                    "target_path": edge["target_path"],
                    "target_title": edge["target_title"],
                    "rendered_line": edge["rendered_line"],
                    "rendered_line_sha256": sha256_bytes(line),
                    "expected_occurrence_count": 1,
                    "current_rendered_collision": False,
                }
            )
        file_records.append(
            {
                "source_topic_id": source_topic_id,
                "source_identity_sha256": sha256_bytes(
                    canonical_json_bytes(
                        (
                            topic_row["topic_id"],
                            topic_row["topic_key"],
                            topic_row["title"],
                            topic_row["obsidian_path"],
                        )
                    )
                ),
                "backup_source_identity_sha256": sha256_bytes(
                    canonical_json_bytes(
                        (
                            topic_row["topic_id"],
                            topic_row["topic_key"],
                            topic_row["title"],
                            topic_row["obsidian_path"],
                        )
                    )
                ),
                "current_source_identity_sha256": sha256_bytes(
                    canonical_json_bytes(current_identity_by_id[source_topic_id])
                ),
                "backup_history_summary_event_count": evidence[
                    "backup_history_event_counts"
                ][source_topic_id],
                "current_history_summary_event_count": evidence[
                    "current_history_event_counts"
                ][source_topic_id],
                "relative_path": source_path,
                "backup_source_path_owner_count": edge_lines[0][0][
                    "backup_source_path_owner_count"
                ],
                "current_source_path_owner_count": edge_lines[0][0][
                    "current_source_path_owner_count"
                ],
                "protected": False,
                "historical_preimage_sha256": sha256_bytes(historical_preimage),
                "historical_preimage_size": len(historical_preimage),
                "pre_sha256": pre_sha256,
                "pre_size": len(observed_preimage),
                "post_sha256": post_sha256,
                "post_size": len(postimage),
                "mode": stat.S_IMODE(info.st_mode),
                "edge_ids": [edge["relation_id"] for edge, _ in edge_lines],
                "line_sha256s": [sha256_bytes(line) for _, line in edge_lines],
                "edges": edges,
            }
        )
        exact_match_count += len(edge_lines)
        total_preimage_bytes += len(observed_preimage)
        total_postimage_bytes += len(postimage)

    file_records.sort(key=lambda record: record["source_topic_id"])
    renderer_source = read_file_bounded(knowledge_module.__file__)
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "canonicalization": "utf8-sorted-keys-compact-no-ascii-escape",
        "generator_commit": generator_commit,
        "renderer_source_sha256": sha256_bytes(renderer_source),
        "inputs": {
            "backup_db": backup_realpath,
            "current_db": current_realpath,
            "vault_root": root_realpath,
        },
        "backup": {
            "path_sha256": sha256_bytes(
                backup_realpath.encode("utf-8")
            ),
            "sha256": evidence["backup_sha256"],
            "mode": evidence["backup_mode"],
            "integrity": evidence["backup_integrity"],
            "selected_edge_set_digest": evidence["selected_edge_digest"],
        },
        "selector": {
            "identifier": "known-broken-updates-reason-v1",
            "reason_sha256": sha256_bytes(KNOWN_BROKEN_RELATION_REASON.encode("utf-8")),
        },
        "vault": {
            "root_realpath_sha256": sha256_bytes(root_realpath.encode("utf-8")),
            "obsidian_subdir": normalized_subdir,
        },
        "counts": {
            "selected": evidence["selected_count"],
            "self_loops": evidence["self_loop_count"],
            "renderable": evidence["renderable_count"],
            "unique_source_files": len(file_records),
        },
        "current": {
            "path_sha256": sha256_bytes(current_realpath.encode("utf-8")),
            "integrity": evidence["current_integrity"],
            "known_invalid_count": evidence["current_known_invalid_count"],
            "counts": evidence["current_counts"],
            "fts_parity": evidence["current_fts_parity"],
            "snapshot_digest": evidence["current_snapshot_digest"],
            "relation_count": evidence["current_relation_count"],
            "relation_set_digest": evidence["current_relation_set_digest"],
            "risky_warning_count": evidence["risky_warning_count"],
            "risky_warning_set_digest": evidence["risky_warning_set_digest"],
        },
        "files": file_records,
    }
    manifest_sha256 = sha256_bytes(canonical_json_bytes(manifest))
    initial_state = {
        "schema": STATE_SCHEMA,
        "manifest_sha256": manifest_sha256,
        "state": "planned",
        "files": {},
        "errors": [],
    }

    _seal_run_artifacts(
        run_path,
        supplied_run_parent,
        supplied_parent_identity,
        manifest,
        initial_state,
    )

    return {
        "applicable": True,
        "selected_count": evidence["selected_count"],
        "self_loop_count": evidence["self_loop_count"],
        "renderable_count": evidence["renderable_count"],
        "exact_match_count": exact_match_count,
        "unique_source_file_count": len(file_records),
        "current_relation_count": evidence["current_relation_count"],
        "current_relation_set_digest": evidence["current_relation_set_digest"],
        "risky_warning_count": evidence["risky_warning_count"],
        "risky_warning_set_digest": evidence["risky_warning_set_digest"],
        "manifest_sha256": manifest_sha256,
        "preimage_bytes": total_preimage_bytes,
        "postimage_bytes": total_postimage_bytes,
        "protected_count": 0,
        "missing_count": 0,
        "duplicate_count": 0,
        "ambiguous_count": 0,
        "drifted_count": 0,
        "live_overlap_count": 0,
        "materialization_timeout_count": 0,
    }


def collect_database_evidence(backup_db, current_db, expectations=LIVE_EXPECTATIONS):
    """Collect canonical backup/current evidence without writing either database."""
    backup_path = os.path.abspath(os.path.expanduser(str(backup_db)))
    current_path = os.path.abspath(os.path.expanduser(str(current_db)))
    try:
        backup_digest = _sha256_file(backup_path)
        backup_mode = stat.S_IMODE(os.stat(backup_path).st_mode)
    except OSError as exc:
        raise CleanupError("backup_unavailable", "verified backup is unavailable") from exc
    if backup_digest != str(expectations.backup_sha256):
        raise CleanupError("backup_checksum", "verified backup checksum mismatch")
    if backup_mode != 0o600:
        raise CleanupError("backup_mode", "verified backup mode must be 0600")

    try:
        backup_conn = open_read_only_db(backup_path, immutable=True)
    except sqlite3.Error as exc:
        raise CleanupError("backup_open", "verified backup could not be opened") from exc
    try:
        backup_integrity = _integrity_check(backup_conn)
        if backup_integrity != "ok":
            raise CleanupError("backup_integrity", "verified backup integrity check failed")
        selected_rows = list(
            backup_conn.execute(INVALID_EDGE_SQL, (KNOWN_BROKEN_RELATION_REASON,))
        )
        selected_relation_rows = list(
            backup_conn.execute(
                """
                SELECT relation_id, source_topic_id, target_topic_id,
                       relation, reason, created_at
                FROM relations
                WHERE relation = 'updates' AND reason = ?
                ORDER BY relation_id
                """,
                (KNOWN_BROKEN_RELATION_REASON,),
            )
        )
        for row in selected_rows:
            _validate_selected_edge(row)
        selected_relation_tuples = [
            _relation_tuple(row, code="backup_value_type")
            for row in selected_relation_rows
        ]
        renderable_source_ids = {
            row["source_topic_id"]
            for row in selected_rows
            if row["source_topic_id"] != row["target_topic_id"]
        }
        backup_sources = {}
        backup_targets = {}
        backup_history_event_counts = {}
        for row in selected_rows:
            source_id = row["source_topic_id"]
            target_id = row["target_topic_id"]
            backup_sources[source_id] = _topic_by_id(backup_conn, source_id)
            backup_targets[target_id] = _topic_by_id(backup_conn, target_id)
            _validate_source_identity(
                backup_sources[source_id], code="backup_value_type"
            )
            _validate_target_identity(
                backup_targets[target_id], code="backup_value_type"
            )
            if source_id not in backup_history_event_counts:
                backup_event_types = [
                    _require_text(
                        event_row[0], code="backup_value_type", field="event_type"
                    )
                    for event_row in backup_conn.execute(
                        "SELECT event_type FROM events WHERE topic_id = ? ORDER BY event_id",
                        (source_id,),
                    )
                ]
                backup_history_event_counts[source_id] = backup_event_types.count(
                    "history_summary"
                )
                backup_source = backup_sources[source_id]
                if (
                    source_id in renderable_source_ids
                    and (
                        (
                            backup_source["topic_key"] is not None
                            and backup_source["topic_key"].startswith("history-summary:")
                        )
                        or "\u5386\u53f2\u603b\u7ed3" in backup_source["title"]
                        or backup_history_event_counts[source_id]
                    )
                ):
                    raise CleanupError(
                        "history_topic", "historical source topic is protected"
                    )
            if _path_owner_count(backup_conn, row["source_path"]) != 1:
                raise CleanupError(
                    "source_path_owner_count",
                    "backup source path ownership is not unique",
                )
    except sqlite3.Error as exc:
        raise CleanupError("backup_schema", "verified backup schema evidence failed") from exc
    finally:
        backup_conn.close()

    self_loop_rows = [
        row
        for row in selected_rows
        if row["source_topic_id"] == row["target_topic_id"]
    ]
    renderable_rows = [
        row
        for row in selected_rows
        if row["source_topic_id"] != row["target_topic_id"]
    ]
    if len(selected_rows) != int(expectations.selected_edges):
        raise CleanupError("selector_count", "selected edge count mismatch")
    selected_ids = [row["relation_id"] for row in selected_rows]
    selected_relation_ids = [row["relation_id"] for row in selected_relation_rows]
    if selected_ids != selected_relation_ids:
        raise CleanupError(
            "selector_accounting",
            "selected edge identity accounting mismatch",
        )
    if len(self_loop_rows) != int(expectations.self_loops):
        raise CleanupError("self_loop_count", "selected self-loop count mismatch")
    if len(renderable_rows) != int(expectations.renderable_edges):
        raise CleanupError("renderable_count", "renderable selected edge count mismatch")

    try:
        current_conn = open_read_only_db(current_path)
    except sqlite3.Error as exc:
        raise CleanupError("current_open", "current database could not be opened") from exc
    try:
        current_conn.execute("BEGIN")
        current_integrity = _integrity_check(current_conn)
        if current_integrity != "ok":
            raise CleanupError("current_integrity", "current database integrity check failed")
        current_known_invalid_count = _count(
            current_conn,
            "SELECT COUNT(*) FROM relations WHERE relation = 'updates' AND reason = ?",
            (KNOWN_BROKEN_RELATION_REASON,),
        )
        if current_known_invalid_count:
            raise CleanupError(
                "current_known_invalid",
                "current database still has known-invalid edges",
            )
        current_relation_rows = list(current_conn.execute(CURRENT_RELATION_SQL))
        current_relation_tuples = [
            _relation_tuple(row, code="current_value_type")
            for row in current_relation_rows
        ]
        current_counts = _database_counts(current_conn)
        current_fts_parity = _fts_parity_evidence(current_conn)
        parity_failures = (
            current_fts_parity["missing_topic_count"],
            current_fts_parity["orphan_topic_id_count"],
            current_fts_parity["duplicate_topic_id_count"],
            current_fts_parity["null_topic_id_count"],
            current_fts_parity["noninteger_topic_id_count"],
        )
        if (
            current_counts["fts"] != current_counts["topics"]
            or current_fts_parity["row_count"] != current_counts["topics"]
            or any(parity_failures)
        ):
            raise CleanupError(
                "current_fts_mismatch",
                "current FTS membership does not match topics",
            )
        fts_topic_ids = [
            _require_integer(row[0], code="current_value_type", field="FTS topic_id")
            for row in current_conn.execute(
                "SELECT topic_id FROM topic_fts ORDER BY topic_id"
            )
        ]
        current_fts_parity["topic_id_digest"] = _canonical_digest(
            fts_topic_ids,
            code="current_canonicalization",
        )
        if current_counts["orphan_events"]:
            raise CleanupError("current_event_orphans", "current database has orphan events")
        if current_counts["orphan_relations"]:
            raise CleanupError(
                "current_relation_orphans",
                "current database has orphan relations",
            )

        current_sources = {}
        current_targets = {}
        current_history_event_counts = {}
        for source_id, backup_source in backup_sources.items():
            current_source = _topic_by_id(current_conn, source_id)
            if backup_source is None or current_source is None:
                raise CleanupError("topic_missing", "selected source topic is missing")
            _validate_source_identity(current_source, code="current_value_type")
            current_event_types = [
                _require_text(
                    row[0], code="current_value_type", field="event_type"
                )
                for row in current_conn.execute(
                    "SELECT event_type FROM events WHERE topic_id = ? ORDER BY event_id",
                    (source_id,),
                )
            ]
            current_history_event_counts[source_id] = current_event_types.count(
                "history_summary"
            )
            if (
                source_id in renderable_source_ids
                and (
                    (
                        current_source["topic_key"] is not None
                        and current_source["topic_key"].startswith("history-summary:")
                    )
                    or "\u5386\u53f2\u603b\u7ed3" in current_source["title"]
                    or current_history_event_counts[source_id]
                )
            ):
                raise CleanupError("history_topic", "current source topic is protected")
            if current_source["obsidian_path"] != backup_source["obsidian_path"]:
                raise CleanupError("source_path_drift", "selected source path drifted")
            if _source_identity_tuple(current_source) != _source_identity_tuple(
                backup_source
            ):
                raise CleanupError(
                    "source_identity_drift", "selected source identity drifted"
                )
            if _path_owner_count(current_conn, backup_source["obsidian_path"]) != 1:
                raise CleanupError(
                    "source_path_owner_count",
                    "current source path ownership is not unique",
                )
            current_sources[source_id] = current_source

        for target_id, backup_target in backup_targets.items():
            current_target = _topic_by_id(current_conn, target_id)
            if backup_target is None or current_target is None:
                raise CleanupError("topic_missing", "selected target topic is missing")
            _validate_target_identity(current_target, code="current_value_type")
            if current_target["obsidian_path"] != backup_target["obsidian_path"]:
                raise CleanupError("target_path_drift", "selected target path drifted")
            if current_target["title"] != backup_target["title"]:
                raise CleanupError("target_title_drift", "selected target title drifted")
            current_targets[target_id] = current_target

        source_identities = [
            _source_identity_tuple(current_sources[topic_id])
            for topic_id in sorted(current_sources)
        ]
        target_identities = [
            _target_identity_tuple(current_targets[topic_id])
            for topic_id in sorted(current_targets)
        ]
        risky_warning_tuples = [
            _relation_tuple(row, code="current_value_type")
            for row in current_conn.execute(
                f"""
                SELECT r.relation_id, r.source_topic_id, r.target_topic_id,
                       r.relation, r.reason, r.created_at
                FROM relations r
                JOIN topics s ON s.topic_id = r.source_topic_id
                JOIN topics t ON t.topic_id = r.target_topic_id
                WHERE {RISKY_CROSS_CHAT_CONDITION_SQL}
                ORDER BY r.relation_id
                """
            )
        ]
        relation_set_digest = _canonical_digest(
            current_relation_tuples,
            code="current_canonicalization",
        )
        risky_warning_set_digest = _canonical_digest(
            risky_warning_tuples,
            code="current_canonicalization",
        )
        current_snapshot = {
            "schema": MANIFEST_SCHEMA,
            "topic_count": current_counts["topics"],
            "event_count": current_counts["events"],
            "fts_count": current_counts["fts"],
            "fts_parity": current_fts_parity,
            "orphan_event_count": current_counts["orphan_events"],
            "orphan_relation_count": current_counts["orphan_relations"],
            "source_identities": source_identities,
            "target_identities": target_identities,
            "relation_set_digest": relation_set_digest,
            "risky_warning_set_digest": risky_warning_set_digest,
        }
        current_snapshot_digest = _canonical_digest(
            current_snapshot,
            code="current_canonicalization",
        )
    except sqlite3.Error as exc:
        raise CleanupError("current_schema", "current database schema evidence failed") from exc
    finally:
        current_conn.close()

    relation_tuple_by_id = {row[0]: row for row in selected_relation_tuples}
    renderable_edges = []
    for row in renderable_rows:
        relation_id = row["relation_id"]
        rendered_line = _render_relation_markdown_line(
            "updates", row["target_path"], row["target_title"]
        ) + "\n"
        renderable_edges.append(
            {
                "relation_id": relation_id,
                "source_topic_id": row["source_topic_id"],
                "target_topic_id": row["target_topic_id"],
                "source_topic_key": row["source_topic_key"],
                "source_title": row["source_title"],
                "source_path": row["source_path"],
                "target_title": row["target_title"],
                "target_path": row["target_path"],
                "rendered_line": rendered_line,
                "rendered_line_sha256": sha256_bytes(rendered_line.encode("utf-8")),
                "relation_tuple": relation_tuple_by_id[relation_id],
                "backup_source_path_owner_count": 1,
                "current_source_path_owner_count": 1,
            }
        )

    return {
        "backup_sha256": backup_digest,
        "backup_mode": backup_mode,
        "backup_integrity": backup_integrity,
        "selected_count": len(selected_rows),
        "self_loop_count": len(self_loop_rows),
        "renderable_count": len(renderable_rows),
        "renderable_edges": renderable_edges,
        "selected_relation_tuples": selected_relation_tuples,
        "selected_edge_digest": _canonical_digest(
            selected_relation_tuples,
            code="backup_canonicalization",
        ),
        "current_integrity": current_integrity,
        "current_known_invalid_count": current_known_invalid_count,
        "current_counts": current_counts,
        "current_fts_parity": current_fts_parity,
        "source_identities": source_identities,
        "backup_history_event_counts": backup_history_event_counts,
        "current_history_event_counts": current_history_event_counts,
        "target_identities": target_identities,
        "current_relation_tuples": current_relation_tuples,
        "current_relation_count": len(current_relation_tuples),
        "current_relation_set_digest": relation_set_digest,
        "risky_warning_tuples": risky_warning_tuples,
        "risky_warning_count": len(risky_warning_tuples),
        "risky_warning_set_digest": risky_warning_set_digest,
        "current_snapshot_digest": current_snapshot_digest,
    }
