"""Read-only counts-and-paths plan for source metadata regeneration."""
from __future__ import annotations

import os
import sqlite3
import time

from .daily_digest import _now_dt, digest_output_path
from .knowledge import KnowledgeStore
from .source_contract import is_history_summary


class SourceMetadataPlanError(RuntimeError):
    """Privacy-safe failure from the read-only source metadata planner."""


def _normalized_path(value):
    return str(value or "").replace("\\", "/")


def plan_source_metadata_regeneration(config: dict, now_func=time.time) -> dict:
    """Return the exact producer-owned rewrite surface without writing it."""
    database_path = os.path.expanduser(str(config.get("monitor_knowledge_db") or ""))
    vault_root = os.path.expanduser(str(config.get("monitor_obsidian_root") or ""))
    if not database_path or not os.path.isfile(database_path):
        raise SourceMetadataPlanError("configured knowledge database is unavailable")

    store = KnowledgeStore.from_config(config, read_only=True)
    conn = None
    try:
        conn = store.connect()
        if conn is None:
            raise SourceMetadataPlanError("configured knowledge database is unavailable")
        rows = conn.execute(
            """
            SELECT t.*,
                   EXISTS(
                       SELECT 1 FROM events e
                       WHERE e.topic_id = t.topic_id
                         AND e.event_type = 'history_summary'
                   ) AS has_history_summary_event
            FROM topics t
            ORDER BY t.topic_id
            """
        ).fetchall()
        atomic_paths = []
        history_summary_paths = []
        for row in rows:
            topic = store._topic_dict(row)
            events = (
                ({"event_type": "history_summary"},)
                if row["has_history_summary_event"]
                else ()
            )
            target = _normalized_path(topic["obsidian_path"])
            if is_history_summary(topic, events):
                history_summary_paths.append(target)
            else:
                atomic_paths.append(target)
    except SourceMetadataPlanError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        raise SourceMetadataPlanError(
            "configured knowledge database is unreadable"
        ) from exc
    finally:
        if conn is not None:
            conn.close()

    try:
        date_plan = store.plan_date_indexes()
        now = _now_dt(config, now_func=now_func)
        digest_path, digest_relative_path = digest_output_path(
            config,
            now.strftime("%Y-%m-%d"),
            now_func=now_func,
        )
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        raise SourceMetadataPlanError("configured producer paths are unreadable") from exc

    date_targets = [dict(target) for target in date_plan.get("targets") or []]
    date_skip_count = sum(
        1 for target in date_targets if target.get("status") == "conflict"
    )
    writable_date_targets = sum(
        1 for target in date_targets if target.get("status") != "conflict"
    )
    daily_digest_paths = [
        _normalized_path(digest_relative_path or digest_path)
    ]
    atomic_paths.sort()
    history_summary_paths.sort()
    return {
        "database_path": database_path,
        "vault_root": vault_root,
        "atomic_paths": atomic_paths,
        "history_summary_paths": history_summary_paths,
        "date_index_targets": date_targets,
        "date_index_conflict_count": int(date_plan.get("conflict_count") or 0),
        "date_index_skip_count": date_skip_count,
        "daily_digest_paths": daily_digest_paths,
        "rewrite_candidate_count": (
            len(atomic_paths)
            + len(history_summary_paths)
            + writable_date_targets
            + len(daily_digest_paths)
        ),
    }
