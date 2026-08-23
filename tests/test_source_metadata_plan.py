import hashlib
import os
import tempfile
import unittest

from core.knowledge import KnowledgeStore


def candidate(**overrides):
    value = {
        "title": "Atomic topic",
        "summary": "Summary",
        "topic_key": "atomic-topic",
        "category": "技术方法",
        "entities": ["Example Entity"],
        "key_facts": ["Fact"],
        "links": [],
        "event_type": "note",
        "status_hint": "tracking",
    }
    value.update(overrides)
    return value


class SourceMetadataPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "knowledge.db")
        self.vault_root = os.path.join(self.tmp.name, "vault")
        self.config = {
            "monitor_knowledge_db": self.db_path,
            "monitor_obsidian_root": self.vault_root,
            "monitor_obsidian_subdir": "关注推送",
            "daily_digest_timezone": "Asia/Shanghai",
        }
        store = KnowledgeStore(
            self.db_path,
            self.vault_root,
            now_func=lambda: 1000,
        )
        messages = [{
            "timestamp": 1,
            "time_str": "2026-07-16 09:00",
            "sender": "Example Sender",
            "text": "fixture",
        }]
        store.apply_event(
            candidate(), messages, {"monitor_chat_display_name": "Fixture Chat"},
            {"relation": "new"},
        )
        store.apply_event(
            candidate(
                title="History",
                topic_key="history-summary:fixture:2026-07-16",
                event_type="history_summary",
            ),
            messages,
            {"monitor_chat_display_name": "Fixture Chat"},
            {"relation": "new"},
        )
        for name in ("reports", "cache", "runtime"):
            directory = os.path.join(self.tmp.name, name)
            os.makedirs(directory)
            with open(os.path.join(directory, "sentinel.txt"), "w", encoding="utf-8") as handle:
                handle.write(name)

    def tearDown(self):
        self.tmp.cleanup()

    def snapshot(self):
        result = {}
        for directory, dirnames, filenames in os.walk(self.tmp.name):
            dirnames.sort()
            for filename in sorted(filenames):
                path = os.path.join(directory, filename)
                with open(path, "rb") as handle:
                    digest = hashlib.sha256(handle.read()).hexdigest()
                result[os.path.relpath(path, self.tmp.name)] = (
                    digest,
                    os.stat(path).st_mtime_ns,
                )
        return result

    def test_plan_is_counts_paths_only_and_zero_mutation(self):
        try:
            from core.source_metadata_plan import plan_source_metadata_regeneration
        except ImportError:
            self.fail("source metadata planner is missing")

        before = self.snapshot()
        plan = plan_source_metadata_regeneration(self.config, now_func=lambda: 1000)
        after = self.snapshot()

        self.assertEqual(after, before)
        self.assertEqual(len(plan["atomic_paths"]), 1)
        self.assertEqual(len(plan["history_summary_paths"]), 1)
        self.assertEqual(len(plan["date_index_targets"]), 2)
        self.assertEqual(len(plan["daily_digest_paths"]), 1)
        self.assertEqual(plan["rewrite_candidate_count"], 5)
        self.assertNotIn("summary", plan)
        self.assertNotIn("entities", plan)
        self.assertNotIn("markdown", plan)

    def test_missing_database_fails_closed(self):
        try:
            from core.source_metadata_plan import (
                SourceMetadataPlanError,
                plan_source_metadata_regeneration,
            )
        except ImportError:
            self.fail("source metadata planner is missing")

        missing = dict(self.config)
        missing["monitor_knowledge_db"] = os.path.join(self.tmp.name, "missing.db")
        with self.assertRaises(SourceMetadataPlanError):
            plan_source_metadata_regeneration(missing, now_func=lambda: 1000)
