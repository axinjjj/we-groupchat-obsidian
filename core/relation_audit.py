"""Relation integrity diagnostics and the exact known-invalid repair primitive."""
from __future__ import annotations

import os
import sqlite3
import time
from urllib.parse import quote

from .config import ensure_private_file


KNOWN_BROKEN_RELATION_REASON = (
    "关系判定失败，按新线索保守提醒: "
    "'TopicMonitor' object has no attribute '_call_deepseek'"
)

CROSS_CHAT_CONDITION_SQL = """
    (
        COALESCE(s.source_chat_username, '') != ''
        AND COALESCE(t.source_chat_username, '') != ''
        AND s.source_chat_username != t.source_chat_username
    ) OR (
        (
            COALESCE(s.source_chat_username, '') = ''
            OR COALESCE(t.source_chat_username, '') = ''
        )
        AND s.source_chat != t.source_chat
    )
"""

RISKY_CROSS_CHAT_CONDITION_SQL = f"""
    ({CROSS_CHAT_CONDITION_SQL})
    AND r.relation IN ('updates', 'contradicts')
    AND lower(r.reason) NOT LIKE '%shared link%'
    AND r.reason NOT LIKE '%共享链接%'
"""


class RelationRepairError(RuntimeError):
    """Raised when an exact relation repair safety invariant fails."""


def _integrity_check(conn):
    rows = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
    return "ok" if rows == ["ok"] else "; ".join(rows)


def _known_invalid_count(conn):
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM relations WHERE relation = 'updates' AND reason = ?",
            (KNOWN_BROKEN_RELATION_REASON,),
        ).fetchone()[0]
    )


def _repair_counts(conn):
    return {
        "topics": int(conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0]),
        "events": int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]),
        "relations": int(conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]),
        "fts": int(conn.execute("SELECT COUNT(*) FROM topic_fts").fetchone()[0]),
        "orphan_events": int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM events e
                LEFT JOIN topics t ON t.topic_id = e.topic_id
                WHERE t.topic_id IS NULL
                """
            ).fetchone()[0]
        ),
        "orphan_relations": int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM relations r
                LEFT JOIN topics s ON s.topic_id = r.source_topic_id
                LEFT JOIN topics t ON t.topic_id = r.target_topic_id
                WHERE s.topic_id IS NULL OR t.topic_id IS NULL
                """
            ).fetchone()[0]
        ),
    }


def _validate_repair_counts(counts, *, label):
    if counts["topics"] <= 0 or counts["events"] <= 0:
        raise RelationRepairError(f"{label}: topics/events must be non-empty")
    if counts["fts"] != counts["topics"]:
        raise RelationRepairError(f"{label}: FTS row count does not match topics")
    if counts["orphan_events"] or counts["orphan_relations"]:
        raise RelationRepairError(f"{label}: orphan rows detected")


def repair_known_invalid_relations(db_path, *, backup_path, expected_count):
    """Delete only exact known-invalid update edges after a verified backup."""
    source_path = os.path.abspath(os.path.expanduser(str(db_path or "")))
    raw_backup_path = str(backup_path or "").strip()
    try:
        expected_count = int(expected_count)
    except (TypeError, ValueError) as exc:
        raise RelationRepairError("expected_count must be a positive integer") from exc
    if expected_count <= 0:
        raise RelationRepairError("expected_count must be a positive integer")
    if not os.path.isfile(source_path):
        raise RelationRepairError("knowledge database is unavailable")
    if not raw_backup_path:
        raise RelationRepairError("backup path is required")
    backup_path = os.path.abspath(os.path.expanduser(raw_backup_path))
    if os.path.lexists(backup_path):
        raise RelationRepairError("backup path already exists")
    os.makedirs(os.path.dirname(backup_path), exist_ok=True)

    source = sqlite3.connect(source_path)
    try:
        integrity_before = _integrity_check(source)
        if integrity_before != "ok":
            raise RelationRepairError(f"source integrity check failed: {integrity_before}")
        before = _repair_counts(source)
        _validate_repair_counts(before, label="source before repair")
        actual_count = _known_invalid_count(source)
        if actual_count != expected_count:
            raise RelationRepairError(
                f"known-invalid count mismatch: expected {expected_count}, found {actual_count}"
            )

        backup = sqlite3.connect(backup_path)
        try:
            source.backup(backup)
        finally:
            backup.close()
        ensure_private_file(backup_path)

        backup = sqlite3.connect(backup_path)
        try:
            backup_integrity = _integrity_check(backup)
            if backup_integrity != "ok":
                raise RelationRepairError(f"backup integrity check failed: {backup_integrity}")
            backup_counts = _repair_counts(backup)
            _validate_repair_counts(backup_counts, label="backup")
            if backup_counts != before:
                raise RelationRepairError("backup counts do not match source")
            if _known_invalid_count(backup) != expected_count:
                raise RelationRepairError("backup known-invalid count does not match source")
        finally:
            backup.close()

        source.execute("BEGIN IMMEDIATE")
        transaction_count = _known_invalid_count(source)
        if transaction_count != expected_count:
            raise RelationRepairError(
                f"known-invalid count drifted before delete: expected {expected_count}, found {transaction_count}"
            )
        cursor = source.execute(
            "DELETE FROM relations WHERE relation = 'updates' AND reason = ?",
            (KNOWN_BROKEN_RELATION_REASON,),
        )
        deleted_count = int(cursor.rowcount)
        if deleted_count != expected_count:
            raise RelationRepairError(
                f"delete count mismatch: expected {expected_count}, deleted {deleted_count}"
            )
        remaining = _known_invalid_count(source)
        if remaining:
            raise RelationRepairError(f"known-invalid rows remain inside transaction: {remaining}")

        after = _repair_counts(source)
        _validate_repair_counts(after, label="source after delete")
        for key in ("topics", "events", "fts"):
            if after[key] != before[key]:
                raise RelationRepairError(f"{key} count changed during relation repair")
        if before["relations"] - after["relations"] != expected_count:
            raise RelationRepairError("total relation count changed by an unexpected amount")
        transaction_integrity = _integrity_check(source)
        if transaction_integrity != "ok":
            raise RelationRepairError(
                f"post-delete integrity check failed: {transaction_integrity}"
            )
        source.commit()

        integrity_after = _integrity_check(source)
        if integrity_after != "ok":
            raise RelationRepairError(f"post-commit integrity check failed: {integrity_after}")
        return {
            "applied": True,
            "backup_path": backup_path,
            "deleted_count": deleted_count,
            "remaining_known_invalid": remaining,
            "topics_before": before["topics"],
            "topics_after": after["topics"],
            "events_before": before["events"],
            "events_after": after["events"],
            "relations_before": before["relations"],
            "relations_after": after["relations"],
            "fts_before": before["fts"],
            "fts_after": after["fts"],
            "integrity_before": integrity_before,
            "integrity_after": integrity_after,
        }
    except Exception:
        if source.in_transaction:
            source.rollback()
        raise
    finally:
        source.close()


def _empty_report(*, available=False, error=""):
    return {
        "available": available,
        "error": error,
        "total_topics": 0,
        "total_events": 0,
        "total_relations": 0,
        "relation_counts": {},
        "known_broken_reason_count": 0,
        "broader_relation_failure_count": 0,
        "recent_relation_failure_count": 0,
        "affected_source_topic_count": 0,
        "affected_target_topic_count": 0,
        "cross_chat_edge_count": 0,
        "cross_chat_risky_edge_count": 0,
        "self_loop_count": 0,
        "exact_replay_group_count": 0,
        "exact_replay_excess_event_count": 0,
        "orphan_event_count": 0,
        "orphan_relation_count": 0,
        "fts_row_count": 0,
        "fts_matches_topics": False,
        "dominant_relation": "",
        "dominant_relation_ratio": 0.0,
        "warnings": [],
        "examples": [],
    }


def _read_only_connection(db_path):
    absolute_path = os.path.abspath(os.path.expanduser(str(db_path or "")))
    uri = f"file:{quote(absolute_path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def audit_relations(
    db_path,
    *,
    sensitive=False,
    example_limit=5,
    known_failure_text=KNOWN_BROKEN_RELATION_REASON,
):
    """Return bounded relation-integrity counts without modifying SQLite."""
    path = os.path.abspath(os.path.expanduser(str(db_path or "")))
    if not path or not os.path.isfile(path):
        return _empty_report(error="knowledge database is unavailable")

    report = _empty_report(available=True)
    try:
        conn = _read_only_connection(path)
    except sqlite3.Error as exc:
        return _empty_report(error=f"{type(exc).__name__}: {exc}")

    try:
        report["total_topics"] = int(conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0])
        report["total_events"] = int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        report["total_relations"] = int(conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0])
        report["relation_counts"] = {
            str(row["relation"]): int(row["count"])
            for row in conn.execute(
                "SELECT relation, COUNT(*) AS count FROM relations GROUP BY relation ORDER BY relation"
            )
        }
        report["known_broken_reason_count"] = int(
            conn.execute(
                "SELECT COUNT(*) FROM relations WHERE relation = 'updates' AND reason = ?",
                (str(known_failure_text or ""),),
            ).fetchone()[0]
        )
        report["broader_relation_failure_count"] = int(
            conn.execute(
                "SELECT COUNT(*) FROM relations WHERE reason LIKE ?",
                ("关系判定失败%",),
            ).fetchone()[0]
        )
        report["recent_relation_failure_count"] = int(
            conn.execute(
                "SELECT COUNT(*) FROM relations WHERE reason LIKE ? AND created_at >= ?",
                ("关系判定失败%", time.time() - 7 * 24 * 60 * 60),
            ).fetchone()[0]
        )
        report["affected_source_topic_count"] = int(
            conn.execute(
                """
                SELECT COUNT(DISTINCT source_topic_id)
                FROM relations
                WHERE relation = 'updates' AND reason = ?
                """,
                (str(known_failure_text or ""),),
            ).fetchone()[0]
        )
        report["affected_target_topic_count"] = int(
            conn.execute(
                """
                SELECT COUNT(DISTINCT target_topic_id)
                FROM relations
                WHERE relation = 'updates' AND reason = ?
                """,
                (str(known_failure_text or ""),),
            ).fetchone()[0]
        )
        report["cross_chat_edge_count"] = int(
            conn.execute(
                f"""
                SELECT COUNT(*)
                FROM relations r
                JOIN topics s ON s.topic_id = r.source_topic_id
                JOIN topics t ON t.topic_id = r.target_topic_id
                WHERE {CROSS_CHAT_CONDITION_SQL}
                """
            ).fetchone()[0]
        )
        report["cross_chat_risky_edge_count"] = int(
            conn.execute(
                f"""
                SELECT COUNT(*)
                FROM relations r
                JOIN topics s ON s.topic_id = r.source_topic_id
                JOIN topics t ON t.topic_id = r.target_topic_id
                WHERE {RISKY_CROSS_CHAT_CONDITION_SQL}
                """
            ).fetchone()[0]
        )
        report["self_loop_count"] = int(
            conn.execute(
                "SELECT COUNT(*) FROM relations WHERE source_topic_id = target_topic_id"
            ).fetchone()[0]
        )
        event_columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(events)")
        }
        replay_columns = {"message_hash", "source_chat", "source_chat_username"}
        if replay_columns.issubset(event_columns):
            replay_row = conn.execute(
                """
                WITH replay_groups AS (
                    SELECT
                        COALESCE(NULLIF(source_chat_username, ''), source_chat) AS chat_key,
                        message_hash,
                        COUNT(*) AS event_count
                    FROM events
                    WHERE COALESCE(message_hash, '') != ''
                    GROUP BY chat_key, message_hash
                    HAVING COUNT(*) > 1
                )
                SELECT
                    COUNT(*) AS group_count,
                    COALESCE(SUM(event_count - 1), 0) AS excess_event_count
                FROM replay_groups
                """
            ).fetchone()
            report["exact_replay_group_count"] = int(replay_row["group_count"])
            report["exact_replay_excess_event_count"] = int(
                replay_row["excess_event_count"]
            )
        report["orphan_event_count"] = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM events e
                LEFT JOIN topics t ON t.topic_id = e.topic_id
                WHERE t.topic_id IS NULL
                """
            ).fetchone()[0]
        )
        report["orphan_relation_count"] = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM relations r
                LEFT JOIN topics s ON s.topic_id = r.source_topic_id
                LEFT JOIN topics t ON t.topic_id = r.target_topic_id
                WHERE s.topic_id IS NULL OR t.topic_id IS NULL
                """
            ).fetchone()[0]
        )
        report["fts_row_count"] = int(conn.execute("SELECT COUNT(*) FROM topic_fts").fetchone()[0])
        report["fts_matches_topics"] = report["fts_row_count"] == report["total_topics"]

        non_duplicate_counts = {
            relation: count
            for relation, count in report["relation_counts"].items()
            if relation != "duplicate_of"
        }
        non_duplicate_total = sum(non_duplicate_counts.values())
        if non_duplicate_counts:
            dominant_relation, dominant_count = max(
                non_duplicate_counts.items(),
                key=lambda item: (item[1], item[0]),
            )
            report["dominant_relation"] = dominant_relation
            if non_duplicate_total:
                report["dominant_relation_ratio"] = dominant_count / non_duplicate_total
            if non_duplicate_total >= 20 and report["dominant_relation_ratio"] > 0.95:
                report["warnings"].append("dominant_relation_ratio")
        if report["known_broken_reason_count"]:
            report["warnings"].append("known_broken_relation_reason")
        if report["cross_chat_risky_edge_count"]:
            report["warnings"].append("cross_chat_relations")
        if report["recent_relation_failure_count"]:
            report["warnings"].append("recent_relation_failure")
        if report["exact_replay_excess_event_count"]:
            report["warnings"].append("exact_message_replays")
        if report["orphan_event_count"] or report["orphan_relation_count"]:
            report["warnings"].append("orphan_rows")
        if not report["fts_matches_topics"]:
            report["warnings"].append("fts_topic_count_mismatch")

        limit = max(0, int(example_limit))
        if limit:
            rows = conn.execute(
                """
                SELECT
                    r.relation_id,
                    r.relation,
                    r.source_topic_id,
                    r.target_topic_id,
                    s.title AS source_title,
                    t.title AS target_title
                FROM relations r
                JOIN topics s ON s.topic_id = r.source_topic_id
                JOIN topics t ON t.topic_id = r.target_topic_id
                WHERE r.relation = 'updates' AND r.reason = ?
                ORDER BY r.relation_id
                LIMIT ?
                """,
                (str(known_failure_text or ""), limit),
            ).fetchall()
            for row in rows:
                example = {
                    "relation_id": int(row["relation_id"]),
                    "relation": str(row["relation"]),
                    "source_topic_id": int(row["source_topic_id"]),
                    "target_topic_id": int(row["target_topic_id"]),
                }
                if sensitive:
                    example["source_title"] = str(row["source_title"])
                    example["target_title"] = str(row["target_title"])
                report["examples"].append(example)
        return report
    except sqlite3.Error as exc:
        return _empty_report(available=True, error=f"{type(exc).__name__}: {exc}")
    finally:
        conn.close()
