import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from core.relation_audit import (
    KNOWN_BROKEN_RELATION_REASON,
    RISKY_CROSS_CHAT_CONDITION_SQL,
    RelationRepairError,
    audit_relations,
    repair_known_invalid_relations,
)
from tests.paths import repo_path


class RelationAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "knowledge.db")
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(
                """
                CREATE TABLE topics (
                    topic_id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL,
                    source_chat TEXT NOT NULL,
                    source_chat_username TEXT NOT NULL
                );
                CREATE TABLE events (
                    event_id INTEGER PRIMARY KEY,
                    topic_id INTEGER NOT NULL,
                    relation TEXT NOT NULL,
                    message_hash TEXT NOT NULL DEFAULT '',
                    source_chat TEXT NOT NULL DEFAULT '',
                    source_chat_username TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE relations (
                    relation_id INTEGER PRIMARY KEY,
                    source_topic_id INTEGER NOT NULL,
                    target_topic_id INTEGER NOT NULL,
                    relation TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE topic_fts (topic_id INTEGER NOT NULL);
                """
            )
            conn.executemany(
                "INSERT INTO topics VALUES (?, ?, ?, ?)",
                [
                    (1, "Alpha secret title", "Alpha Chat", "alpha@chatroom"),
                    (2, "Beta secret title", "Beta Chat", "beta@chatroom"),
                ],
            )
            conn.executemany(
                "INSERT INTO events (event_id, topic_id, relation) VALUES (?, ?, ?)",
                [(1, 1, "new"), (2, 2, "new")],
            )
            conn.executemany("INSERT INTO topic_fts VALUES (?)", [(1,), (2,)])
            relations = []
            for relation_id in range(1, 21):
                source = 1 if relation_id % 2 else 2
                target = 2 if source == 1 else 1
                reason = KNOWN_BROKEN_RELATION_REASON if relation_id <= 3 else "ordinary relation reason"
                relations.append((relation_id, source, target, "updates", reason, float(relation_id)))
            relations.append((21, 1, 1, "related", "self edge", 21.0))
            conn.executemany("INSERT INTO relations VALUES (?, ?, ?, ?, ?, ?)", relations)
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _digest(path):
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()

    def test_audit_reports_relation_integrity_without_mutating_database(self):
        before = self._digest(self.db_path)

        audit = audit_relations(self.db_path)

        self.assertEqual(audit["total_topics"], 2)
        self.assertEqual(audit["total_events"], 2)
        self.assertEqual(audit["total_relations"], 21)
        self.assertEqual(audit["relation_counts"], {"related": 1, "updates": 20})
        self.assertEqual(audit["known_broken_reason_count"], 3)
        self.assertEqual(audit["broader_relation_failure_count"], 3)
        self.assertEqual(audit["affected_source_topic_count"], 2)
        self.assertEqual(audit["affected_target_topic_count"], 2)
        self.assertEqual(audit["cross_chat_edge_count"], 20)
        self.assertEqual(audit["cross_chat_risky_edge_count"], 20)
        self.assertEqual(audit["self_loop_count"], 1)
        self.assertEqual(audit["exact_replay_group_count"], 0)
        self.assertEqual(audit["exact_replay_excess_event_count"], 0)
        self.assertEqual(audit["orphan_event_count"], 0)
        self.assertEqual(audit["orphan_relation_count"], 0)
        self.assertEqual(audit["fts_row_count"], 2)
        self.assertTrue(audit["fts_matches_topics"])
        self.assertEqual(audit["dominant_relation"], "updates")
        self.assertGreater(audit["dominant_relation_ratio"], 0.9)
        self.assertIn("dominant_relation_ratio", audit["warnings"])
        self.assertNotIn("Alpha secret title", repr(audit["examples"]))
        self.assertEqual(before, self._digest(self.db_path))

    def test_audit_reports_exact_message_replays_by_stable_chat(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executemany(
                """
                INSERT INTO events (
                    event_id, topic_id, relation, message_hash,
                    source_chat, source_chat_username
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (3, 1, "new", "same-hash", "Alpha Chat", "alpha@chatroom"),
                    (4, 1, "duplicate", "same-hash", "Alpha Chat", "alpha@chatroom"),
                    (5, 2, "new", "same-hash", "Beta Chat", "beta@chatroom"),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        audit = audit_relations(self.db_path)

        self.assertEqual(audit["exact_replay_group_count"], 1)
        self.assertEqual(audit["exact_replay_excess_event_count"], 1)
        self.assertIn("exact_message_replays", audit["warnings"])

    def test_shared_risky_cross_chat_predicate_matches_audit_count(self):
        conn = sqlite3.connect(self.db_path)
        try:
            count = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM relations r
                JOIN topics s ON s.topic_id = r.source_topic_id
                JOIN topics t ON t.topic_id = r.target_topic_id
                WHERE {RISKY_CROSS_CHAT_CONDITION_SQL}
                """
            ).fetchone()[0]
        finally:
            conn.close()

        self.assertEqual(count, audit_relations(self.db_path)["cross_chat_risky_edge_count"])

    def test_exact_repair_deletes_only_proven_invalid_updates(self):
        backup_path = os.path.join(self.tmp.name, "before-repair.db")
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO relations VALUES (22, 1, 2, 'related', ?, 22)",
                (KNOWN_BROKEN_RELATION_REASON,),
            )
            conn.commit()
        finally:
            conn.close()

        result = repair_known_invalid_relations(
            self.db_path,
            backup_path=backup_path,
            expected_count=3,
        )

        self.assertEqual(result["deleted_count"], 3)
        self.assertEqual(result["remaining_known_invalid"], 0)
        self.assertEqual(result["relations_before"] - result["relations_after"], 3)
        self.assertEqual(result["topics_before"], result["topics_after"])
        self.assertEqual(result["events_before"], result["events_after"])
        self.assertEqual(result["fts_before"], result["fts_after"])
        self.assertEqual(result["integrity_before"], "ok")
        self.assertEqual(result["integrity_after"], "ok")
        if os.name == "nt":
            from core.windows_permissions import is_private_to_current_user

            self.assertTrue(is_private_to_current_user(backup_path))
        else:
            self.assertEqual(os.stat(backup_path).st_mode & 0o777, 0o600)

        backup_audit = audit_relations(backup_path)
        source_audit = audit_relations(self.db_path)
        self.assertEqual(backup_audit["known_broken_reason_count"], 3)
        self.assertEqual(source_audit["known_broken_reason_count"], 0)

        conn = sqlite3.connect(self.db_path)
        try:
            ordinary_updates = conn.execute(
                "SELECT COUNT(*) FROM relations WHERE relation = 'updates' AND reason = 'ordinary relation reason'"
            ).fetchone()[0]
            same_reason_related = conn.execute(
                "SELECT COUNT(*) FROM relations WHERE relation = 'related' AND reason = ?",
                (KNOWN_BROKEN_RELATION_REASON,),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(ordinary_updates, 17)
        self.assertEqual(same_reason_related, 1)

    def test_exact_repair_refuses_count_mismatch_without_backup_or_mutation(self):
        backup_path = os.path.join(self.tmp.name, "count-mismatch.db")
        before = self._digest(self.db_path)

        with self.assertRaisesRegex(RelationRepairError, "count mismatch"):
            repair_known_invalid_relations(
                self.db_path,
                backup_path=backup_path,
                expected_count=4,
            )

        self.assertFalse(os.path.exists(backup_path))
        self.assertEqual(before, self._digest(self.db_path))
        self.assertEqual(audit_relations(self.db_path)["known_broken_reason_count"], 3)

    def test_exact_repair_refuses_existing_backup_without_mutation(self):
        backup_path = os.path.join(self.tmp.name, "existing-backup.db")
        with open(backup_path, "w", encoding="utf-8") as handle:
            handle.write("do not overwrite")
        before = self._digest(self.db_path)

        with self.assertRaisesRegex(RelationRepairError, "already exists"):
            repair_known_invalid_relations(
                self.db_path,
                backup_path=backup_path,
                expected_count=3,
            )

        self.assertEqual(before, self._digest(self.db_path))
        with open(backup_path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "do not overwrite")

    def test_exact_repair_requires_nonempty_backup_path_without_mutation(self):
        before = self._digest(self.db_path)

        with self.assertRaisesRegex(RelationRepairError, "backup path is required"):
            repair_known_invalid_relations(
                self.db_path,
                backup_path="",
                expected_count=3,
            )

        self.assertEqual(before, self._digest(self.db_path))

    def test_exact_repair_rolls_back_when_post_delete_integrity_fails(self):
        backup_path = os.path.join(self.tmp.name, "rollback-backup.db")
        before = self._digest(self.db_path)

        with patch(
            "core.relation_audit._integrity_check",
            side_effect=["ok", "ok", "forced failure"],
        ):
            with self.assertRaisesRegex(RelationRepairError, "post-delete integrity"):
                repair_known_invalid_relations(
                    self.db_path,
                    backup_path=backup_path,
                    expected_count=3,
                )

        self.assertTrue(os.path.exists(backup_path))
        self.assertEqual(before, self._digest(self.db_path))
        self.assertEqual(audit_relations(self.db_path)["known_broken_reason_count"], 3)
        self.assertEqual(audit_relations(backup_path)["known_broken_reason_count"], 3)

    def test_sensitive_audit_includes_bounded_titles(self):
        audit = audit_relations(self.db_path, sensitive=True, example_limit=1)

        self.assertEqual(len(audit["examples"]), 1)
        self.assertEqual(audit["examples"][0]["source_title"], "Alpha secret title")
        self.assertEqual(audit["examples"][0]["target_title"], "Beta secret title")

    def test_cross_chat_count_accepts_legacy_empty_username_with_same_display_name(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO topics VALUES (3, 'Legacy Alpha', 'Alpha Chat', '')"
            )
            conn.execute(
                "INSERT INTO relations VALUES (22, 1, 3, 'related', 'legacy same chat', 22)"
            )
            conn.commit()
        finally:
            conn.close()

        audit = audit_relations(self.db_path)

        self.assertEqual(audit["cross_chat_edge_count"], 20)

    def test_known_broken_selector_requires_update_relation(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO relations VALUES (22, 1, 2, 'related', ?, 22)",
                (KNOWN_BROKEN_RELATION_REASON,),
            )
            conn.commit()
        finally:
            conn.close()

        audit = audit_relations(self.db_path)

        self.assertEqual(audit["known_broken_reason_count"], 3)
        self.assertEqual(audit["broader_relation_failure_count"], 4)

    def test_safe_cross_chat_related_edge_does_not_raise_risky_cross_chat_warning(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("DELETE FROM relations")
            conn.execute(
                "INSERT INTO relations VALUES (1, 1, 2, 'related', 'shared link evidence', 1)"
            )
            conn.commit()
        finally:
            conn.close()

        audit = audit_relations(self.db_path)

        self.assertEqual(audit["cross_chat_edge_count"], 1)
        self.assertEqual(audit["cross_chat_risky_edge_count"], 0)
        self.assertNotIn("cross_chat_relations", audit["warnings"])

    def test_audit_reports_orphans_and_fts_mismatch(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO events (event_id, topic_id, relation) VALUES (3, 999, 'new')"
            )
            conn.execute("INSERT INTO relations VALUES (22, 999, 2, 'updates', 'orphan', 22)")
            conn.execute("DELETE FROM topic_fts WHERE topic_id = 2")
            conn.commit()
        finally:
            conn.close()

        audit = audit_relations(self.db_path)

        self.assertEqual(audit["orphan_event_count"], 1)
        self.assertEqual(audit["orphan_relation_count"], 1)
        self.assertEqual(audit["fts_row_count"], 1)
        self.assertFalse(audit["fts_matches_topics"])

    def test_missing_database_returns_unavailable_report_without_creating_file(self):
        missing = os.path.join(self.tmp.name, "missing.db")

        audit = audit_relations(missing)

        self.assertFalse(audit["available"])
        self.assertFalse(os.path.exists(missing))

    def test_audit_cli_is_json_capable_and_apply_mode_is_narrowly_named(self):
        script = repo_path("scripts", "repair_relations.py")

        result = subprocess.run(
            [sys.executable, script, "audit", "--db", self.db_path, "--json"],
            check=False,
            capture_output=True,
            text=True,
        )
        help_result = subprocess.run(
            [sys.executable, script, "--help"],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["known_broken_reason_count"], 3)
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("apply-known-invalid", help_result.stdout)
        self.assertNotIn("--apply", help_result.stdout)
        self.assertIn("audit-only", help_result.stdout)

    def test_apply_cli_requires_exact_confirmation(self):
        script = repo_path("scripts", "repair_relations.py")
        backup_path = os.path.join(self.tmp.name, "wrong-confirmation.db")
        before = self._digest(self.db_path)

        result = subprocess.run(
            [
                sys.executable,
                script,
                "apply-known-invalid",
                "--db",
                self.db_path,
                "--backup",
                backup_path,
                "--expect-count",
                "3",
                "--confirm",
                "wrong",
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("confirmation", result.stderr.lower())
        self.assertFalse(os.path.exists(backup_path))
        self.assertEqual(before, self._digest(self.db_path))

    def test_apply_cli_runs_exact_repair(self):
        script = repo_path("scripts", "repair_relations.py")
        backup_path = os.path.join(self.tmp.name, "cli-backup.db")

        result = subprocess.run(
            [
                sys.executable,
                script,
                "apply-known-invalid",
                "--db",
                self.db_path,
                "--backup",
                backup_path,
                "--expect-count",
                "3",
                "--confirm",
                "DELETE_EXACT_KNOWN_INVALID_RELATIONS",
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["deleted_count"], 3)
        self.assertEqual(report["remaining_known_invalid"], 0)
        self.assertTrue(os.path.exists(backup_path))
        self.assertEqual(audit_relations(self.db_path)["known_broken_reason_count"], 0)


if __name__ == "__main__":
    unittest.main()
