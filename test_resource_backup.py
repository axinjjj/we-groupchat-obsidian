import fcntl
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from core.resource_backup import MountedResourceBackup
from core.resource_capture import SelectedResourceCapture, _url_sha256
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
            "google_drive_file_sync_selected_chats": [
                {"username": self.selected, "alias": "猫猫研究群"},
                {"username": self.ghost, "alias": "stale selection"},
            ],
            "google_drive_file_sync_max_messages_per_scan": 50,
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

    def test_resource_index_groups_coobserved_links_and_files_without_copying_bytes(self):
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
        self.assertIn("同条消息共同出现，内容关联未确认", text)
        self.assertIn("https://example.com/A?token=secret-value", text)
        self.assertIn("report.pdf", text)
        self.assertIn("Drive handoff：sync_delegated", text)
        vault_files = [
            os.path.join(dirpath, filename)
            for dirpath, _dirs, filenames in os.walk(self.obsidian_root)
            for filename in filenames
        ]
        self.assertTrue(all(path.endswith(".md") for path in vault_files))

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
        with open(root_path, "w", encoding="utf-8") as handle:
            handle.write("# user-owned root\n")
        with open(month_path, "w", encoding="utf-8") as handle:
            handle.write("# user-owned month\n")

        result = backup.render_obsidian_indexes()

        self.assertEqual(result["state"], "written")
        with open(root_path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "# user-owned root\n")
        with open(month_path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "# user-owned month\n")
        generated_root = os.path.join(chat_root, "00-资源索引.generated.md")
        generated_month = os.path.join(month_root, month + ".generated.md")
        self.assertTrue(os.path.isfile(generated_root))
        self.assertTrue(os.path.isfile(generated_month))
        with open(generated_root, encoding="utf-8") as handle:
            root_text = handle.read()
        self.assertIn(f"[[资源索引/{month}.generated|{month}]]", root_text)

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
        config["google_drive_file_sync_selected_chats"] = [
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


if __name__ == "__main__":
    unittest.main()
