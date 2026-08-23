import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import scripts.resource_backup as resource_backup_cli
from core.resource_backup import MountedResourceBackup, _redact_url
from core.resource_capture import SelectedResourceCapture, _exact_links, _url_sha256
from core.wechat_db import WeChatSourceDegraded


class FakeSource:
    def __init__(self, messages_by_chat=None, *, degraded=False):
        self.messages_by_chat = dict(messages_by_chat or {})
        self.degraded = degraded

    def get_message_shards(self, _username):
        if self.degraded:
            raise WeChatSourceDegraded("source_shard_unavailable")
        return ["shard-1"]

    def get_messages_for_shard(
        self,
        username,
        _source_shard_id,
        since_ts=0,
        limit=500,
        page_forward=False,
        since_inclusive=False,
    ):
        if self.degraded:
            raise WeChatSourceDegraded("source_shard_unavailable")
        comparison = (
            (lambda value: value >= since_ts)
            if since_inclusive
            else (lambda value: value > since_ts)
        )
        rows = [
            dict(row)
            for row in self.messages_by_chat.get(username, [])
            if comparison(int(row.get("timestamp") or 0))
        ]
        rows.sort(key=lambda row: int(row.get("timestamp") or 0))
        if not page_forward:
            rows = rows[-limit:]
        return rows[:limit]


class ResourceBackupTests(unittest.TestCase):
    def setUp(self):
        self.settings_patcher = patch(
            "core.resource_backup.load_resource_backup_settings",
            return_value={"target": "", "link_export_mode": "redacted"},
        )
        self.settings_patcher.start()
        self.addCleanup(self.settings_patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.db_dir = os.path.join(
            self.root, "xwechat_files", "account", "db_storage"
        )
        self.file_month = os.path.join(
            os.path.dirname(self.db_dir), "msg", "file", "2026-08"
        )
        os.makedirs(self.file_month, exist_ok=True)
        self.archive_root = os.path.join(self.root, "archive")
        self.capture_db = os.path.join(self.root, "resource_capture.db")
        self.knowledge_db = os.path.join(self.root, "knowledge.db")
        self.obsidian_root = os.path.join(self.root, "obsidian")
        self.target = os.path.join(self.root, "Google Drive", "WeChat Archive")
        os.makedirs(self.target, exist_ok=True)
        self.selected = "selected@chatroom"
        self.unselected = "unselected@chatroom"
        self.ghost = "not-monitored@chatroom"
        self.config = {
            "db_dir": self.db_dir,
            "resource_capture_db": self.capture_db,
            "attachment_archive_root": self.archive_root,
            "attachment_archive_min_free_bytes": 0,
            "monitor_knowledge_db": self.knowledge_db,
            "monitor_obsidian_root": self.obsidian_root,
            "monitor_obsidian_subdir": "微信群聊/关注推送",
            "resource_backup_target": self.target,
            "monitor_chats": [
                {"username": self.selected, "name": "Selected Chat"},
                {"username": self.unselected, "name": "Unselected Chat"},
            ],
            "monitor_chat_aliases": {
                self.selected: "猫猫研究群",
                self.unselected: "不外发群",
            },
            "resource_backup_selected_chats": [
                {"username": self.selected, "alias": "猫猫研究群"},
                {"username": self.ghost, "alias": "stale selection"},
            ],
            "resource_backup_max_messages_per_scan": 50,
        }

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _digest(data):
        return hashlib.sha256(data).hexdigest()

    def _write_candidate_files(self):
        first = b"first file bytes"
        second = b"second file bytes"
        with open(os.path.join(self.file_month, "report.pdf"), "wb") as handle:
            handle.write(first)
        with open(os.path.join(self.file_month, "report (1).pdf"), "wb") as handle:
            handle.write(second)
        return first, second

    def _selected_message(self):
        first, second = self._write_candidate_files()
        first_url = "https://example.com/A?token=secret-value"
        second_url = "https://example.com/a?x=1"
        return {
            "timestamp": 1_787_481_060,
            "time_str": "2026-08-23 10:31",
            "sender": "Faye",
            "text": f"资源 {first_url} {second_url} {first_url}",
            "source_message_id": "wgmsg_selected_resource_fixture",
            "resources": [
                {
                    "kind": "file",
                    "resource_index": 0,
                    "original_name": "report.pdf",
                    "declared_size": len(first),
                    "declared_hash": self._digest(first),
                },
                {
                    "kind": "file",
                    "resource_index": 1,
                    "original_name": "report.pdf",
                    "declared_size": len(second),
                    "declared_hash": self._digest(second),
                },
            ],
        }

    def _unselected_message(self):
        return {
            "timestamp": 1_787_481_061,
            "time_str": "2026-08-23 10:32",
            "sender": "Someone",
            "text": "https://private.example/unselected",
            "source_message_id": "wgmsg_unselected_fixture",
            "resources": [],
        }

    def _capture(self, *, degraded=False):
        source = FakeSource(
            {
                self.selected: [self._selected_message()],
                self.unselected: [self._unselected_message()],
            },
            degraded=degraded,
        )
        return SelectedResourceCapture(
            self.config,
            source=source,
            now_func=lambda: 1_787_500_000,
            random_func=lambda: 0.5,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000001",
        )

    def _ready_capture(self):
        capture = self._capture()
        capture.initialize_selected_chat_cursors(start_timestamp=0)
        scanned = capture.scan()
        self.assertEqual(scanned["state"], "healthy")
        self.assertEqual(scanned["captured_links"], 2)
        self.assertEqual(scanned["captured_files"], 2)
        resolved = capture.resolve_pending_files(limit=10)
        self.assertEqual(resolved["ready_local"], 2)
        return capture

    def _backup(self, capture, *, mode="redacted"):
        return MountedResourceBackup(
            self.config,
            capture=capture,
            now_func=lambda: 1_787_600_000,
            id_factory=lambda: "fixture",
            link_export_mode=mode,
        )

    def test_scope_is_active_monitor_intersection_explicit_selection(self):
        capture = self._capture()
        chats = capture.selected_chats()
        self.assertEqual(len(chats), 1)
        self.assertEqual(chats[0]["username"], self.selected)
        self.assertEqual(chats[0]["alias"], "猫猫研究群")

        capture.initialize_selected_chat_cursors(start_timestamp=0)
        capture.scan()
        rows = capture.occurrences(selected_only=False)
        self.assertEqual({row["chat_username"] for row in rows}, {self.selected})
        self.assertNotIn("unselected", json.dumps(rows))

    def test_exact_links_and_same_name_distinct_file_bytes_are_first_class_occurrences(self):
        capture = self._ready_capture()
        rows = capture.occurrences()
        self.assertEqual(len(rows), 4)
        links = [row for row in rows if row["kind"] == "link"]
        files = [row for row in rows if row["kind"] == "file"]
        self.assertEqual(
            [row["observed_url"] for row in links],
            [
                "https://example.com/A?token=secret-value",
                "https://example.com/a?x=1",
            ],
        )
        self.assertEqual(
            [row["url_sha256"] for row in links],
            [_url_sha256(row["observed_url"]) for row in links],
        )
        self.assertEqual(len({row["object_sha256"] for row in files}), 2)
        self.assertEqual({row["original_name"] for row in files}, {"report.pdf"})
        self.assertEqual(
            {row["source_message_id"] for row in rows},
            {"wgmsg_selected_resource_fixture"},
        )

    def test_exact_link_identity_preserves_terminal_url_characters(self):
        urls = [
            "https://example.com/report!",
            "https://example.com/path?x=1;",
            "https://example.com/a:b",
        ]

        self.assertEqual(_exact_links(" ".join(urls)), urls)

    def test_mounted_handoff_exports_only_selected_occurrences_and_redacts_tokens(self):
        capture = self._ready_capture()
        # Add an unrelated local CAS object. It exists locally but has no selected
        # occurrence, so it must never be copied by this lane.
        capture.archive.ensure_layout()
        unselected_digest, _size, _relpath = capture.archive.store_bytes(
            b"unselected local object", "unselected.bin"
        )

        backup = self._backup(capture)
        result = backup.run()

        self.assertEqual(result["state"], "sync_delegated")
        self.assertEqual(result["copied"], 2)
        snapshot_id = result["snapshot"]["snapshot_id"]
        snapshot_dir = os.path.join(
            backup.backup_root, "snapshots", snapshot_id
        )
        with open(os.path.join(snapshot_dir, "resources.jsonl"), encoding="utf-8") as handle:
            records = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual(len(records), 4)
        serialized = json.dumps(records, ensure_ascii=False)
        self.assertNotIn(self.unselected, serialized)
        self.assertNotIn(self.ghost, serialized)
        self.assertNotIn(unselected_digest, serialized)
        self.assertNotIn("secret-value", serialized)
        self.assertIn("REDACTED", serialized)
        self.assertTrue(all(record["chat_alias"] == "猫猫研究群" for record in records))
        self.assertTrue(all(
            re.fullmatch(r"wgo_resource_[0-9a-f]{32}", record["occurrence_id"])
            for record in records
        ))

        objects_root = os.path.join(backup.backup_root, "objects", "sha256")
        copied_files = [
            os.path.join(dirpath, filename)
            for dirpath, _dirs, filenames in os.walk(objects_root)
            for filename in filenames
        ]
        self.assertEqual(len(copied_files), 2)
        self.assertFalse(any(unselected_digest in path for path in copied_files))

    def test_resource_index_is_a_light_resource_list_without_sender_or_ledger_details(self):
        capture = self._ready_capture()
        backup = self._backup(capture)
        backup.run()

        month_note = os.path.join(
            self.obsidian_root,
            "微信群聊",
            "关注推送",
            "猫猫研究群",
            "资源索引",
            "2026-08.md",
        )
        # Find the generated month rather than binding the test to one timezone.
        index_dir = os.path.dirname(month_note)
        month_files = [
            os.path.join(index_dir, name)
            for name in os.listdir(index_dir)
            if name.endswith(".md")
        ]
        self.assertEqual(len(month_files), 1)
        with open(month_files[0], encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn(
            "[https://example.com/A?token=secret-value]"
            "(<https://example.com/A?token=secret-value>)",
            text,
        )
        self.assertIn("📎 [report.pdf]", text)
        self.assertNotIn("Faye", text)
        self.assertNotIn("URL identity", text)
        self.assertNotIn("SHA-256", text)
        self.assertNotIn("Drive handoff", text)
        self.assertNotIn("同条消息共同出现", text)
        self.assertNotIn("source_message_id", text)
        vault_files = [
            os.path.join(dirpath, filename)
            for dirpath, _dirs, filenames in os.walk(self.obsidian_root)
            for filename in filenames
        ]
        self.assertTrue(all(path.endswith(".md") for path in vault_files))

    def test_resource_index_prefers_observed_title_and_uses_exact_url_as_fallback(self):
        capture = self._ready_capture()
        rows = capture.occurrences()
        links = [row for row in rows if row["kind"] == "link"]
        links[0]["original_name"] = "Observed title"
        links[1]["original_name"] = ""
        backup = self._backup(capture)

        text = backup._render_month(
            "猫猫研究群",
            "2026-08",
            links,
            {},
            target_view=False,
        )

        self.assertIn(
            "[Observed title](<https://example.com/A?token=secret-value>)",
            text,
        )
        self.assertIn(
            "[https://example.com/a?x=1](<https://example.com/a?x=1>)",
            text,
        )
        self.assertNotIn("[链接]", text)

    def test_resource_indexes_have_a_discoverable_scope_root(self):
        capture = self._ready_capture()
        backup = self._backup(capture)

        result = backup.render_obsidian_indexes()

        self.assertEqual(result["state"], "written")
        scope_root = os.path.join(
            self.obsidian_root,
            "微信群聊",
            "关注推送",
            "00-资源索引.md",
        )
        with open(scope_root, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("<!-- we-groupchat-obsidian:resource-index v1 -->", text)
        self.assertIn("[[猫猫研究群/00-资源索引|猫猫研究群]]", text)
        self.assertIn("2 个链接 · 2 个文件", text)

    def test_ordinary_rerun_trusts_delivery_receipt_and_does_not_rehash_target_objects(self):
        capture = self._ready_capture()
        backup = self._backup(capture)
        first = backup.run()
        self.assertEqual(first["copied"], 2)

        original_hash = __import__("core.resource_backup", fromlist=["_hash_path"])._hash_path

        def reject_target_rehash(path):
            if os.path.commonpath((os.path.abspath(path), backup.backup_root)) == backup.backup_root:
                raise AssertionError("scheduled rerun hydrated/rehashed mounted target")
            return original_hash(path)

        with patch("core.resource_backup._hash_path", side_effect=reject_target_rehash):
            second = backup.run()
        self.assertEqual(second["state"], "idle")
        self.assertEqual(second["copied"], 0)
        self.assertEqual(second["reused"], 2)

    def test_explicit_verify_rehashes_and_detects_target_corruption(self):
        capture = self._ready_capture()
        backup = self._backup(capture)
        completed = backup.run()
        snapshot_id = completed["snapshot"]["snapshot_id"]
        delivery = next(iter(backup._delivery_map().values()))
        object_path = os.path.join(backup.backup_root, delivery["target_relpath"])
        with open(object_path, "wb") as handle:
            handle.write(b"corrupt")

        verified = backup.verify(snapshot_id)
        self.assertEqual(verified["state"], "target_failed")
        self.assertEqual(verified["failed"], 1)
        self.assertFalse(verified["remote_verified"])

    def test_degraded_shard_does_not_advance_cursor(self):
        capture = self._capture(degraded=True)
        capture.initialize_selected_chat_cursors(start_timestamp=123)
        result = capture.scan()
        self.assertEqual(result["state"], "source_degraded")
        conn = sqlite3.connect(self.capture_db)
        try:
            count = conn.execute("SELECT COUNT(*) FROM resource_occurrences").fetchone()[0]
            shard_count = conn.execute("SELECT COUNT(*) FROM resource_shards").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 0)
        self.assertEqual(shard_count, 0)

    def test_failed_shard_recovers_after_healthy_shard_advances_without_losing_file(self):
        class TwoShardSource:
            fail_a = True

            @staticmethod
            def get_message_shards(_username):
                return ["shard-a", "shard-b"]

            def get_messages_for_shard(self, _username, source_shard_id, **_kwargs):
                if source_shard_id == "shard-a":
                    if self.fail_a:
                        raise WeChatSourceDegraded("source_shard_unavailable")
                    return [{
                        "timestamp": 100,
                        "source_message_id": "message-a-file",
                        "text": "",
                        "resources": [{
                            "kind": "file",
                            "resource_index": 0,
                            "original_name": "a.pdf",
                        }],
                    }]
                return [{
                    "timestamp": 200,
                    "source_message_id": "message-b-link",
                    "text": "https://example.com/b",
                    "resources": [],
                }]

        source = TwoShardSource()
        capture = SelectedResourceCapture(
            self.config,
            source=source,
            now_func=lambda: 1_000,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000001",
        )
        capture.initialize_selected_chat_cursors(start_timestamp=0)

        first = capture.scan()
        source.fail_a = False
        second = capture.scan()
        third = capture.scan()

        self.assertEqual(first["state"], "source_degraded")
        self.assertEqual(second["state"], "healthy")
        self.assertEqual(third["state"], "healthy")
        rows = capture.occurrences()
        self.assertEqual(
            [(row["source_message_id"], row["kind"]) for row in rows],
            [("message-a-file", "file"), ("message-b-link", "link")],
        )
        conn = sqlite3.connect(self.capture_db)
        try:
            cursors = dict(conn.execute(
                "SELECT source_shard_id, cursor_timestamp FROM resource_shards"
            ))
        finally:
            conn.close()
        self.assertEqual(cursors, {"shard-a": 100, "shard-b": 200})

    def test_reselect_starts_new_selection_epoch_without_gap_backfill(self):
        config = dict(self.config)
        config["resource_backup_selected_chats"] = [{
            "username": self.selected,
            "alias": "猫猫研究群",
            "selected_since": 10,
        }]
        source = FakeSource({
            self.selected: [{
                "timestamp": 100,
                "source_message_id": "before-deselect",
                "text": "https://example.com/before",
                "resources": [],
            }],
        })
        capture = SelectedResourceCapture(
            config,
            source=source,
            now_func=lambda: 200,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000001",
        )
        capture.initialize_selected_chat_cursors(start_timestamp=0)
        capture.scan()

        source.messages_by_chat[self.selected].extend([
            {
                "timestamp": 150,
                "source_message_id": "selection-gap",
                "text": "https://example.com/gap",
                "resources": [],
            },
            {
                "timestamp": 250,
                "source_message_id": "after-reselect",
                "text": "https://example.com/after",
                "resources": [],
            },
        ])
        capture.config["resource_backup_selected_chats"] = [{
            "username": self.selected,
            "alias": "猫猫研究群",
            "selected_since": 200,
        }]

        capture.scan()

        self.assertEqual(
            [row["source_message_id"] for row in capture.occurrences()],
            ["before-deselect", "after-reselect"],
        )
        conn = sqlite3.connect(self.capture_db)
        try:
            chat = conn.execute(
                "SELECT selected_since, selection_epoch FROM resource_chats"
            ).fetchone()
            cursor = conn.execute(
                "SELECT cursor_timestamp FROM resource_shards"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(chat, (200, 2))
        self.assertEqual(cursor, 250)

    def test_explicit_backfill_applies_history_without_moving_live_cursor(self):
        source = FakeSource({
            self.selected: [{
                "timestamp": 100,
                "source_message_id": "historical-resource",
                "text": "https://example.com/history",
                "resources": [{
                    "kind": "file",
                    "resource_index": 0,
                    "original_name": "history.pdf",
                }],
            }],
        })
        capture = SelectedResourceCapture(
            self.config,
            source=source,
            now_func=lambda: 200,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000001",
        )
        capture.initialize_selected_chat_cursors(start_timestamp=200)
        capture.scan()

        planned = capture.backfill(0, apply=False)
        applied = capture.backfill(0, apply=True)

        self.assertEqual(planned["state"], "planned")
        self.assertEqual(planned["discovered_links"], 1)
        self.assertEqual(planned["discovered_files"], 1)
        self.assertEqual(capture.backfill(0, apply=True)["inserted_links"], 0)
        self.assertEqual(applied["inserted_links"], 1)
        self.assertEqual(applied["inserted_files"], 1)
        conn = sqlite3.connect(self.capture_db)
        try:
            cursor = conn.execute(
                "SELECT cursor_timestamp FROM resource_shards"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(cursor, 200)

    def test_backfill_uses_cursor_complete_pages_across_filtered_rows(self):
        messages = []
        for timestamp in range(1, 121):
            text = ""
            if timestamp in {10, 60, 110}:
                text = f"https://example.com/history/{timestamp}"
            messages.append({
                "timestamp": timestamp,
                "source_message_id": f"source-{timestamp}",
                "text": text,
                "resources": [],
            })

        class CursorCompleteSource(FakeSource):
            def get_messages_for_shard(self, *args, **kwargs):
                return [
                    row
                    for row in super().get_messages_for_shard(*args, **kwargs)
                    if row.get("text")
                ]

            def get_cursor_messages_for_shard(self, *args, **kwargs):
                return super().get_messages_for_shard(*args, **kwargs)

        capture = SelectedResourceCapture(
            self.config,
            source=CursorCompleteSource({self.selected: messages}),
            now_func=lambda: 200,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000001",
        )

        planned = capture.backfill(0, apply=False)

        self.assertEqual(planned["state"], "planned")
        self.assertEqual(planned["scanned"], 120)
        self.assertEqual(planned["discovered_links"], 3)

    def test_links_only_backfill_never_queues_attachment_files(self):
        source = FakeSource({
            self.selected: [{
                "timestamp": 100,
                "source_message_id": "historical-resource",
                "text": "https://example.com/history",
                "resources": [{
                    "kind": "file",
                    "resource_index": 0,
                    "original_name": "history.pdf",
                }],
            }],
        })
        capture = SelectedResourceCapture(
            self.config,
            source=source,
            now_func=lambda: 200,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000001",
        )

        result = capture.backfill_links(0, apply=True)

        self.assertEqual(result["state"], "applied")
        self.assertEqual(result["mode"], "links_only")
        self.assertTrue(result["source_complete"])
        self.assertEqual(result["discovered_links"], 1)
        self.assertEqual(result["discovered_files"], 0)
        self.assertEqual(result["inserted_links"], 1)
        self.assertEqual(result["inserted_files"], 0)
        self.assertEqual(
            [(row["kind"], row["observed_url"]) for row in capture.occurrences()],
            [("link", "https://example.com/history")],
        )

    def test_links_only_backfill_writes_nothing_when_a_later_shard_degrades(self):
        class PartialSource(FakeSource):
            def get_message_shards(self, _username):
                return ["healthy", "failed"]

            def get_messages_for_shard(
                self,
                username,
                source_shard_id,
                since_ts=0,
                limit=500,
                page_forward=False,
                since_inclusive=False,
            ):
                if source_shard_id == "failed":
                    raise WeChatSourceDegraded("source_shard_unavailable")
                return super().get_messages_for_shard(
                    username,
                    source_shard_id,
                    since_ts=since_ts,
                    limit=limit,
                    page_forward=page_forward,
                    since_inclusive=since_inclusive,
                )

        capture = SelectedResourceCapture(
            self.config,
            source=PartialSource({
                self.selected: [{
                    "timestamp": 100,
                    "source_message_id": "visible-link",
                    "text": "https://example.com/must-not-partially-commit",
                    "resources": [],
                }],
            }),
            now_func=lambda: 200,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000001",
        )

        result = capture.backfill_links(0, apply=True)

        self.assertEqual(result["state"], "source_degraded")
        self.assertFalse(result["source_complete"])
        self.assertEqual(result["discovered_links"], 1)
        self.assertEqual(result["inserted_links"], 0)
        self.assertEqual(capture.occurrences(), [])

    def test_capture_run_reports_source_unavailable_instead_of_healthy(self):
        capture = SelectedResourceCapture(
            self.config,
            source=None,
            now_func=lambda: 1_787_500_000,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000001",
        )
        capture.initialize_selected_chat_cursors(start_timestamp=0)

        result = capture.run(resolve_limit=10)

        self.assertEqual(result["state"], "source_unavailable")
        self.assertEqual(result["scan"]["state"], "source_unavailable")

    def test_capture_run_can_skip_attachment_cache_without_resolver_access(self):
        capture = self._capture()
        capture.initialize_selected_chat_cursors(start_timestamp=0)

        with patch.object(
            capture,
            "resolve_pending_files",
            side_effect=AssertionError("must not touch attachment cache"),
        ):
            result = capture.run(resolve_files=False)

        self.assertEqual(result["resolve"]["state"], "skipped")
        self.assertEqual(result["resolve"]["reason"], "file_resolution_disabled")

    def test_occurrence_insert_and_cursor_advance_are_one_transaction(self):
        capture = self._capture()
        capture.initialize_selected_chat_cursors(start_timestamp=0)

        with patch.object(capture, "_insert_occurrences", side_effect=RuntimeError("boom")):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                capture.scan()

        conn = sqlite3.connect(self.capture_db)
        conn.row_factory = sqlite3.Row
        try:
            shard = conn.execute("SELECT * FROM resource_shards").fetchone()
            occurrence_count = conn.execute(
                "SELECT COUNT(*) FROM resource_occurrences"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertIsNotNone(shard)
        self.assertEqual(int(shard["cursor_timestamp"]), 0)
        self.assertEqual(occurrence_count, 0)


    def test_missing_mounted_target_is_not_recreated(self):
        capture = self._ready_capture()
        missing = os.path.join(self.root, "CloudStorage", "missing-mount")
        config = dict(self.config)
        config["resource_backup_target"] = missing
        backup = MountedResourceBackup(
            config,
            capture=capture,
            now_func=lambda: 1_787_600_000,
            id_factory=lambda: "fixture",
        )

        result = backup.run()

        self.assertEqual(result["state"], "destination_unavailable")
        self.assertFalse(os.path.exists(missing))
        self.assertEqual(result["obsidian"]["state"], "written")
        chat_root = os.path.join(
            self.obsidian_root,
            "微信群聊",
            "关注推送",
            "猫猫研究群",
        )
        self.assertTrue(os.path.isfile(os.path.join(chat_root, "00-资源索引.md")))

    def test_unresolved_files_keep_catalog_snapshot_but_do_not_claim_sync_delegated(self):
        capture = self._capture()
        capture.initialize_selected_chat_cursors(start_timestamp=0)
        capture.scan()
        for filename in os.listdir(self.file_month):
            os.unlink(os.path.join(self.file_month, filename))
        resolved = capture.resolve_pending_files(limit=10)
        self.assertEqual(resolved["ready_local"], 0)
        self.assertEqual(resolved["failed"], 2)
        backup = self._backup(capture)

        result = backup.run()

        self.assertEqual(result["state"], "pending_resources")
        self.assertEqual(result["copied"], 0)
        self.assertEqual(result["unresolved_files"], 2)
        self.assertEqual(result["snapshot"]["state"], "written")
        snapshot_dir = os.path.join(
            backup.backup_root, "snapshots", result["snapshot"]["snapshot_id"]
        )
        with open(os.path.join(snapshot_dir, "manifest.json"), encoding="utf-8") as handle:
            manifest = json.load(handle)
        self.assertEqual(manifest["snapshot_completeness"], "catalog_complete")
        self.assertEqual(manifest["handoff_semantics"], "pending_resources")
        self.assertEqual(manifest["unresolved_file_count"], 2)
        self.assertTrue(os.path.isfile(os.path.join(snapshot_dir, "COMPLETE")))

    def test_unchanged_catalog_rebuilds_deleted_snapshot(self):
        capture = self._ready_capture()
        identifiers = iter(("first", "second"))
        backup = MountedResourceBackup(
            self.config,
            capture=capture,
            now_func=lambda: 1_787_600_000,
            id_factory=lambda: next(identifiers),
        )
        first = backup.run()
        shutil.rmtree(os.path.join(
            backup.backup_root, "snapshots", first["snapshot"]["snapshot_id"]
        ))

        second = backup.run()

        self.assertEqual(second["snapshot"]["state"], "written")
        self.assertNotEqual(
            second["snapshot"]["snapshot_id"], first["snapshot"]["snapshot_id"]
        )
        self.assertIsNotNone(backup._load_snapshot())

    def test_unchanged_catalog_rebuilds_snapshot_without_valid_complete(self):
        capture = self._ready_capture()
        identifiers = iter(("first", "second"))
        backup = MountedResourceBackup(
            self.config,
            capture=capture,
            now_func=lambda: 1_787_600_000,
            id_factory=lambda: next(identifiers),
        )
        first = backup.run()
        complete = os.path.join(
            backup.backup_root, "snapshots", first["snapshot"]["snapshot_id"],
            "COMPLETE",
        )
        os.unlink(complete)

        second = backup.run()

        self.assertEqual(second["snapshot"]["state"], "written")
        self.assertNotEqual(
            second["snapshot"]["snapshot_id"], first["snapshot"]["snapshot_id"]
        )
        self.assertIsNotNone(backup._load_snapshot())

    def test_snapshot_fingerprint_includes_link_export_mode(self):
        source = FakeSource({
            self.selected: [{
                "timestamp": 100,
                "source_message_id": "plain-link",
                "text": "https://example.com/plain?topic=one",
                "resources": [],
            }],
        })
        capture = SelectedResourceCapture(
            self.config,
            source=source,
            now_func=lambda: 200,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000001",
        )
        capture.initialize_selected_chat_cursors(start_timestamp=0)
        capture.scan()
        identifiers = iter(("full", "redacted"))
        full = MountedResourceBackup(
            self.config, capture=capture, link_export_mode="full",
            now_func=lambda: 300, id_factory=lambda: next(identifiers),
        )
        first = full.run()
        redacted = MountedResourceBackup(
            self.config, capture=capture, link_export_mode="redacted",
            now_func=lambda: 300, id_factory=lambda: next(identifiers),
        )

        second = redacted.run()

        self.assertEqual(second["snapshot"]["state"], "written")
        self.assertNotEqual(
            second["snapshot"]["snapshot_id"], first["snapshot"]["snapshot_id"]
        )
        self.assertEqual(
            redacted._load_snapshot()["manifest"]["link_export_mode"], "redacted"
        )

    def test_redacted_mode_covers_authorization_and_credential_variants(self):
        url = (
            "https://example.com/file?authorization=Bearer-secret"
            "&credential=credential-secret&credentials=plural-secret"
            "&jwt=jwt-secret&x-amz-credential=amz-secret"
            "&x-amz-signature=signed-secret&safe=visible"
        )

        redacted = _redact_url(url)

        for secret in (
            "Bearer-secret", "credential-secret", "plural-secret",
            "jwt-secret", "amz-secret", "signed-secret",
        ):
            self.assertNotIn(secret, redacted)
        self.assertIn("safe=visible", redacted)
        self.assertGreaterEqual(redacted.count("REDACTED"), 6)

    def test_malformed_matched_url_fails_closed_without_crashing(self):
        malformed = "https://example.com／evil?token=secret-value"

        self.assertEqual(_redact_url(malformed), "REDACTED_INVALID_URL")
        source = FakeSource({
            self.selected: [{
                "timestamp": 100,
                "source_message_id": "malformed-link",
                "text": malformed,
                "resources": [],
            }],
        })
        capture = SelectedResourceCapture(
            self.config,
            source=source,
            now_func=lambda: 200,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000001",
        )
        capture.initialize_selected_chat_cursors(start_timestamp=0)
        capture.scan()
        backup = self._backup(capture)

        result = backup.run()

        self.assertEqual(result["state"], "sync_delegated")
        snapshot = backup._load_snapshot()
        with open(
            os.path.join(snapshot["directory"], "resources.jsonl"),
            encoding="utf-8",
        ) as handle:
            exported = handle.read()
        self.assertIn("REDACTED_INVALID_URL", exported)
        self.assertNotIn("secret-value", exported)

    def test_unresolved_and_unconfigured_runs_have_nonzero_cli_exit_status(self):
        for state in (
            "pending_resources",
            "target_not_configured",
            "no_selected_chats",
        ):
            with self.subTest(state=state):
                self.assertEqual(resource_backup_cli._exit_code({"state": state}), 2)

    def test_unmanaged_obsidian_indexes_are_preserved_with_generated_fallbacks(self):
        capture = self._ready_capture()
        backup = self._backup(capture)
        month = capture.occurrences()[0]["source_month"]
        chat_root = os.path.join(
            self.obsidian_root,
            "微信群聊",
            "关注推送",
            "猫猫研究群",
        )
        month_root = os.path.join(chat_root, "资源索引")
        os.makedirs(month_root, exist_ok=True)
        root_path = os.path.join(chat_root, "00-资源索引.md")
        month_path = os.path.join(month_root, month + ".md")
        scope_root = os.path.join(
            self.obsidian_root,
            "微信群聊",
            "关注推送",
            "00-资源索引.md",
        )
        with open(root_path, "w", encoding="utf-8") as handle:
            handle.write("# user-owned root\n")
        with open(month_path, "w", encoding="utf-8") as handle:
            handle.write("# user-owned month\n")
        with open(scope_root, "w", encoding="utf-8") as handle:
            handle.write("# user-owned scope root\n")

        result = backup.render_obsidian_indexes()

        self.assertEqual(result["state"], "written")
        with open(root_path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "# user-owned root\n")
        with open(month_path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "# user-owned month\n")
        with open(scope_root, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "# user-owned scope root\n")
        generated_root = os.path.join(chat_root, "00-资源索引.generated.md")
        generated_month = os.path.join(month_root, month + ".generated.md")
        generated_scope_root = os.path.join(
            self.obsidian_root,
            "微信群聊",
            "关注推送",
            "00-资源索引.generated.md",
        )
        self.assertTrue(os.path.isfile(generated_root))
        self.assertTrue(os.path.isfile(generated_month))
        self.assertTrue(os.path.isfile(generated_scope_root))
        with open(generated_root, encoding="utf-8") as handle:
            root_text = handle.read()
        self.assertIn(f"[[资源索引/{month}.generated|{month}]]", root_text)
        with open(generated_scope_root, encoding="utf-8") as handle:
            scope_text = handle.read()
        self.assertIn(
            "[[猫猫研究群/00-资源索引.generated|猫猫研究群]]",
            scope_text,
        )

    def test_delivery_receipt_does_not_trust_a_symlink_replacement(self):
        capture = self._ready_capture()
        backup = self._backup(capture)
        first = backup.run()
        self.assertEqual(first["state"], "sync_delegated")
        delivery = next(iter(backup._delivery_map().values()))
        target_path = os.path.join(backup.backup_root, delivery["target_relpath"])
        replacement = os.path.join(self.root, "replacement.bin")
        with open(replacement, "wb") as handle:
            handle.write(b"replacement")
        os.unlink(target_path)
        os.symlink(replacement, target_path)

        second = backup.run()

        self.assertEqual(second["state"], "target_failed")
        self.assertTrue(
            {"target_object_conflict", "target_outside_backup_root"}
            & set(second["error_codes"])
        )
        self.assertTrue(os.path.islink(target_path))

    def test_insufficient_target_space_keeps_local_state_and_skips_snapshot(self):
        capture = self._ready_capture()
        config = dict(self.config)
        config["resource_backup_min_free_bytes"] = 1
        backup = MountedResourceBackup(
            config,
            capture=capture,
            now_func=lambda: 1_787_600_000,
            id_factory=lambda: "fixture",
        )
        usage = type("Usage", (), {"free": 0})()

        with patch("core.resource_backup.shutil.disk_usage", return_value=usage):
            result = backup.run()

        self.assertEqual(result["state"], "target_failed")
        self.assertIn("insufficient_target_space", result["error_codes"])
        self.assertEqual(result["obsidian"]["state"], "written")
        self.assertFalse(os.path.isdir(os.path.join(backup.backup_root, "snapshots")))

    def test_duplicate_chat_aliases_get_stable_distinct_index_directories(self):
        other = "other-selected@chatroom"
        config = dict(self.config)
        config["monitor_chats"] = [
            {"username": self.selected, "name": "Same Alias"},
            {"username": other, "name": "Same Alias"},
        ]
        config["resource_backup_selected_chats"] = [
            {"username": self.selected, "alias": "Same Alias"},
            {"username": other, "alias": "Same Alias"},
        ]
        second_message = dict(self._unselected_message())
        second_message["source_message_id"] = "wgmsg_other_selected"
        source = FakeSource({
            self.selected: [self._selected_message()],
            other: [second_message],
        })
        capture = SelectedResourceCapture(
            config,
            source=source,
            now_func=lambda: 1_787_500_000,
            random_func=lambda: 0.5,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000001",
        )
        capture.initialize_selected_chat_cursors(start_timestamp=0)
        capture.scan()
        capture.resolve_pending_files(limit=10)
        backup = MountedResourceBackup(
            config,
            capture=capture,
            now_func=lambda: 1_787_600_000,
            id_factory=lambda: "fixture",
        )
        backup.render_obsidian_indexes()
        chat_root = os.path.join(
            self.obsidian_root, "微信群聊", "关注推送"
        )
        directories = sorted(
            name for name in os.listdir(chat_root)
            if os.path.isdir(os.path.join(chat_root, name))
        )
        self.assertEqual(len(directories), 2)
        self.assertTrue(all(name.startswith("Same Alias--") for name in directories))
        self.assertNotEqual(directories[0], directories[1])

    def test_concurrent_handoff_returns_worker_busy(self):
        capture = self._ready_capture()
        backup = self._backup(capture)
        lock_path = self.capture_db + ".resource-backup.lock"
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = backup.run()
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

        self.assertEqual(result["state"], "worker_busy")
        self.assertEqual(result["copied"], 0)

    def test_target_month_index_uses_correct_relative_object_link(self):
        capture = self._ready_capture()
        backup = self._backup(capture)
        backup.run()
        target_index_root = os.path.join(
            backup.backup_root, "views", "猫猫研究群", "资源索引"
        )
        month_file = next(
            os.path.join(target_index_root, name)
            for name in os.listdir(target_index_root)
            if name.endswith(".md")
        )
        with open(month_file, encoding="utf-8") as handle:
            text = handle.read()

        self.assertIn("../../../objects/sha256/", text)

    def test_target_overlap_fails_closed(self):
        capture = self._ready_capture()
        unsafe = dict(self.config)
        unsafe["resource_backup_target"] = self.archive_root
        backup = MountedResourceBackup(
            unsafe,
            capture=capture,
            now_func=lambda: 1_787_600_000,
            id_factory=lambda: "fixture",
        )
        result = backup.run()
        self.assertEqual(result["state"], "invalid_target")
        self.assertEqual(result["error_code"], "target_overlaps_local_source")

    def test_target_subtree_symlink_escape_is_rejected_before_external_write(self):
        capture = self._ready_capture()
        outside = os.path.join(self.root, "outside")
        os.makedirs(outside)
        os.symlink(outside, os.path.join(self.target, "wgo-resource-backup"))
        backup = self._backup(capture)

        plan = backup.plan()
        result = backup.run()

        self.assertEqual(plan["state"], "invalid_target")
        self.assertEqual(plan["error_code"], "target_directory_conflict")
        self.assertEqual(result["state"], "invalid_target")
        self.assertEqual(result["error_code"], "target_directory_conflict")
        self.assertFalse(os.path.exists(os.path.join(outside, "v3")))

    def test_plan_rejects_symlink_at_objects_snapshots_and_views(self):
        capture = self._ready_capture()
        for relative in ("objects", "snapshots", "views"):
            with self.subTest(relative=relative):
                branch_target = os.path.join(self.root, "target-" + relative)
                outside = os.path.join(self.root, "outside-" + relative)
                os.makedirs(os.path.join(branch_target, "wgo-resource-backup", "v3"))
                os.makedirs(outside)
                os.symlink(
                    outside,
                    os.path.join(branch_target, "wgo-resource-backup", "v3", relative),
                )
                config = dict(self.config)
                config["resource_backup_target"] = branch_target
                backup = MountedResourceBackup(config, capture=capture)

                plan = backup.plan()

                self.assertEqual(plan["state"], "invalid_target")
                self.assertEqual(plan["error_code"], "target_subtree_conflict")

    def test_run_returns_target_failed_for_snapshot_directory_conflict(self):
        capture = self._ready_capture()
        backup = self._backup(capture)
        backup._ensure_target_dir(backup.backup_root)
        with open(os.path.join(backup.backup_root, "snapshots"), "wb") as handle:
            handle.write(b"conflict")

        result = backup.run()

        self.assertEqual(result["state"], "target_failed")
        self.assertIn("target_subtree_conflict", result["error_codes"])
        self.assertIsNone(backup._state_row())

    def test_run_returns_target_failed_for_target_view_conflict(self):
        capture = self._ready_capture()
        backup = self._backup(capture)
        backup._ensure_target_dir(backup.backup_root)
        with open(os.path.join(backup.backup_root, "views"), "wb") as handle:
            handle.write(b"conflict")

        result = backup.run()

        self.assertEqual(result["state"], "target_failed")
        self.assertIn("target_subtree_conflict", result["error_codes"])
        self.assertIsNone(backup._state_row())

    def test_status_handoff_semantics_are_derived_from_current_evidence(self):
        capture = self._ready_capture()

        no_target_config = dict(self.config)
        no_target_config["resource_backup_target"] = ""
        no_target = MountedResourceBackup(no_target_config, capture=capture)
        self.assertEqual(
            no_target.status()["handoff_semantics"], "target_not_configured"
        )

        pending = self._backup(capture)
        self.assertEqual(pending.status()["handoff_semantics"], "pending")
        completed = pending.run()
        self.assertEqual(completed["state"], "sync_delegated")
        self.assertEqual(pending.status()["handoff_semantics"], "sync_delegated")

        snapshot_dir = pending._snapshot_dir()
        os.unlink(os.path.join(snapshot_dir, "COMPLETE"))
        self.assertEqual(pending.status()["handoff_semantics"], "pending")

        unresolved_config = dict(self.config)
        unresolved_config["resource_capture_db"] = os.path.join(
            self.root, "unresolved-resource-capture.db"
        )
        unresolved_config["attachment_archive_root"] = os.path.join(
            self.root, "unresolved-archive"
        )
        unresolved_capture = SelectedResourceCapture(
            unresolved_config,
            source=FakeSource({self.selected: [self._selected_message()]}),
            now_func=lambda: 1_787_500_000,
            random_func=lambda: 0.5,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000002",
        )
        unresolved_capture.initialize_selected_chat_cursors(start_timestamp=0)
        unresolved_capture.scan()
        for filename in os.listdir(self.file_month):
            os.unlink(os.path.join(self.file_month, filename))
        unresolved_capture.resolve_pending_files(limit=10)
        unresolved_target = os.path.join(self.root, "unresolved-target")
        os.makedirs(unresolved_target)
        unresolved_config["resource_backup_target"] = unresolved_target
        unresolved = MountedResourceBackup(
            unresolved_config, capture=unresolved_capture
        )
        self.assertEqual(
            unresolved.status()["handoff_semantics"], "pending_resources"
        )

    def test_raw_chat_username_never_crosses_export_boundary(self):
        config = dict(self.config)
        config["monitor_chats"] = [{
            "username": self.selected,
            "name": self.selected,
        }]
        config["monitor_chat_aliases"] = {}
        config["resource_backup_selected_chats"] = [{
            "username": self.selected,
            "alias": self.selected,
        }]
        capture = SelectedResourceCapture(
            config,
            source=FakeSource({self.selected: [self._selected_message()]}),
            now_func=lambda: 1_787_500_000,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000001",
        )
        capture.initialize_selected_chat_cursors(start_timestamp=0)
        capture.scan()
        capture.resolve_pending_files(limit=10)
        backup = MountedResourceBackup(config, capture=capture)

        result = backup.run()

        self.assertNotIn("@chatroom", json.dumps(result, ensure_ascii=False))
        snapshot = backup._load_snapshot()
        with open(
            os.path.join(snapshot["directory"], "resources.jsonl"), encoding="utf-8"
        ) as handle:
            self.assertNotIn("@chatroom", handle.read())
        self.assertTrue(all(
            "@chatroom" not in path
            for path, _dirs, _files in os.walk(backup.backup_root)
        ))

    def test_casefold_equivalent_aliases_get_distinct_paths(self):
        other = "other-selected@chatroom"
        config = dict(self.config)
        config["monitor_chats"] = [
            {"username": self.selected, "name": "Research"},
            {"username": other, "name": "research"},
        ]
        config["resource_backup_selected_chats"] = [
            {"username": self.selected, "alias": "Research"},
            {"username": other, "alias": "research"},
        ]
        source = FakeSource({
            self.selected: [self._selected_message()],
            other: [{
                "timestamp": 100,
                "source_message_id": "casefold-other",
                "text": "https://example.com/other",
                "resources": [],
            }],
        })
        capture = SelectedResourceCapture(
            config,
            source=source,
            now_func=lambda: 1_787_500_000,
            archive_id_factory=lambda: "00000000-0000-0000-0000-000000000001",
        )
        capture.initialize_selected_chat_cursors(start_timestamp=0)
        capture.scan()
        backup = MountedResourceBackup(config, capture=capture)

        backup.render_obsidian_indexes()

        root = os.path.join(self.obsidian_root, "微信群聊", "关注推送")
        directories = sorted(
            name for name in os.listdir(root)
            if os.path.isdir(os.path.join(root, name))
        )
        self.assertEqual(len(directories), 2)
        self.assertTrue(all("--" in name for name in directories))

    def test_configured_target_symlink_is_rejected(self):
        capture = self._ready_capture()
        actual = os.path.join(self.root, "actual-target")
        target_link = os.path.join(self.root, "target-link")
        os.makedirs(actual)
        os.symlink(actual, target_link)
        config = dict(self.config)
        config["resource_backup_target"] = target_link
        backup = MountedResourceBackup(config, capture=capture)

        plan = backup.plan()
        result = backup.run()

        self.assertEqual(plan["state"], "invalid_target")
        self.assertEqual(plan["error_code"], "target_is_symlink")
        self.assertEqual(result["state"], "invalid_target")
        self.assertFalse(os.path.exists(os.path.join(actual, "wgo-resource-backup")))


class ResourceBackupCliTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "monitor_chats": [
                {"username": "first@chatroom", "name": "First Group"},
                {"username": "second@chatroom", "name": "Second Group"},
            ],
            "monitor_chat_aliases": {"second@chatroom": "猫猫研究群"},
            "resource_backup_selected_chats": [
                {"username": "second@chatroom", "alias": "猫猫研究群"},
            ],
            "google_drive_file_sync_selected_chats": [
                {"username": "legacy@chatroom", "alias": "Legacy OAuth Group"},
            ],
        }

    def test_list_chats_is_privacy_safe_and_marks_current_selection(self):
        output = io.StringIO()
        with (
            patch.object(resource_backup_cli, "load_config", return_value=self.config),
            redirect_stdout(output),
        ):
            result = resource_backup_cli.main(["list-chats"])

        payload = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(
            payload["chats"],
            [
                {"index": 1, "alias": "First Group", "selected": False},
                {"index": 2, "alias": "猫猫研究群", "selected": True},
            ],
        )
        self.assertNotIn("@chatroom", output.getvalue())

    def test_set_selected_chats_uses_active_list_indexes_without_oauth_state(self):
        output = io.StringIO()
        with (
            patch.object(resource_backup_cli, "load_config", return_value=self.config),
            patch.object(resource_backup_cli, "save_config") as save_config,
            patch.object(resource_backup_cli.time, "time", return_value=1_787_500_000),
            redirect_stdout(output),
        ):
            result = resource_backup_cli.main(["set-selected-chats", "1"])

        saved = save_config.call_args.args[0]
        self.assertEqual(result, 0)
        self.assertEqual(
            saved["resource_backup_selected_chats"],
            [{
                "username": "first@chatroom",
                "alias": "First Group",
                "selected_since": 1_787_500_000,
            }],
        )
        self.assertEqual(
            saved["google_drive_file_sync_selected_chats"],
            self.config.get("google_drive_file_sync_selected_chats"),
        )
        self.assertNotIn("@chatroom", output.getvalue())

    def test_cli_scan_source_unavailable_returns_structured_json(self):
        output = io.StringIO()
        with (
            patch.object(resource_backup_cli, "load_config", return_value=self.config),
            patch.object(resource_backup_cli, "_source", return_value=None),
            patch.object(resource_backup_cli, "_capture") as capture_factory,
            redirect_stdout(output),
        ):
            capture_factory.return_value.scan.return_value = {
                "state": "source_unavailable",
                "error_code": "source_unavailable",
            }
            result = resource_backup_cli.main(["scan"])

        self.assertEqual(result, 2)
        self.assertEqual(json.loads(output.getvalue())["state"], "source_unavailable")

    def test_source_unavailable_is_a_local_state_not_system_exit(self):
        with patch.object(resource_backup_cli, "get_cached_keys", return_value={}):
            self.assertIsNone(resource_backup_cli._source({"db_dir": ""}))

    def test_backfill_all_is_an_explicit_all_history_plan(self):
        output = io.StringIO()
        capture = unittest.mock.Mock()
        capture.backfill.return_value = {
            "state": "planned",
            "discovered_links": 3,
            "inserted_links": 0,
        }
        with (
            patch.object(resource_backup_cli, "load_config", return_value=self.config),
            patch.object(resource_backup_cli, "_capture", return_value=capture),
            redirect_stdout(output),
        ):
            result = resource_backup_cli.main(["backfill", "--all"])

        self.assertEqual(result, 0)
        capture.backfill.assert_called_once_with(0, apply=False)
        self.assertEqual(json.loads(output.getvalue())["state"], "planned")

    def test_cli_backfill_links_is_an_explicit_links_only_plan(self):
        output = io.StringIO()
        capture = unittest.mock.Mock()
        capture.backfill_links.return_value = {
            "state": "planned",
            "mode": "links_only",
            "source_complete": True,
            "discovered_links": 3,
            "inserted_links": 0,
        }
        with (
            patch.object(resource_backup_cli, "load_config", return_value=self.config),
            patch.object(resource_backup_cli, "_capture", return_value=capture),
            redirect_stdout(output),
        ):
            result = resource_backup_cli.main(["backfill-links", "--all"])

        self.assertEqual(result, 0)
        capture.backfill_links.assert_called_once_with(0, apply=False)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["mode"], "links_only")
        self.assertTrue(payload["source_complete"])

    def test_cli_enables_long_lived_updates_without_enabling_file_access(self):
        output = io.StringIO()
        config = dict(self.config)
        config["resource_backup_enabled"] = False
        config["resource_backup_file_resolution_enabled"] = False
        with (
            patch.object(resource_backup_cli, "load_config", return_value=config),
            patch.object(resource_backup_cli, "save_config") as save,
            redirect_stdout(output),
        ):
            result = resource_backup_cli.main(["enable"])

        self.assertEqual(result, 0)
        saved = save.call_args.args[0]
        self.assertTrue(saved["resource_backup_enabled"])
        self.assertFalse(saved["resource_backup_file_resolution_enabled"])
        self.assertEqual(json.loads(output.getvalue())["runtime"], "long_lived_app")

    def test_cli_run_with_source_unavailable_still_handoffs(self):
        output = io.StringIO()
        capture = type("Capture", (), {})()
        capture.run = lambda resolve_limit=50, resolve_files=True: {
            "state": "source_unavailable",
            "scan": {"state": "source_unavailable"},
            "resolve": {"state": "healthy", "ready_local": 1},
        }
        backup = type("Backup", (), {})()
        backup.run = lambda: {"state": "sync_delegated", "copied": 1}
        with (
            patch.object(resource_backup_cli, "load_config", return_value=self.config),
            patch.object(resource_backup_cli, "_source", return_value=None),
            patch.object(resource_backup_cli, "_capture", return_value=capture),
            patch.object(resource_backup_cli, "_backup", return_value=backup) as backup_factory,
            redirect_stdout(output),
        ):
            result = resource_backup_cli.main(["run"])

        payload = json.loads(output.getvalue())
        self.assertEqual(result, 2)
        self.assertEqual(payload["state"], "source_unavailable")
        self.assertEqual(payload["backup"]["state"], "sync_delegated")
        backup_factory.assert_called_once()

    def test_cli_run_skips_file_resolution_unless_explicitly_requested(self):
        output = io.StringIO()
        capture = unittest.mock.Mock()
        capture.run.return_value = {
            "state": "healthy",
            "scan": {"state": "healthy"},
            "resolve": {"state": "skipped"},
        }
        backup = unittest.mock.Mock()
        backup.run.return_value = {"state": "idle"}
        with (
            patch.object(resource_backup_cli, "load_config", return_value=self.config),
            patch.object(resource_backup_cli, "_capture", return_value=capture),
            patch.object(resource_backup_cli, "_backup", return_value=backup),
            redirect_stdout(output),
        ):
            result = resource_backup_cli.main(["run"])

        self.assertEqual(result, 0)
        capture.run.assert_called_once_with(resolve_limit=50, resolve_files=False)


if __name__ == "__main__":
    unittest.main()
