import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import unittest

from core.attachment_archive import AttachmentArchive
from core.attachment_backup import AttachmentBackup, BACKUP_SCHEMA
from core.knowledge import KnowledgeStore


class AttachmentBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "knowledge.db")
        self.archive_root = os.path.join(self.tmp.name, "archive")
        self.target = os.path.join(self.tmp.name, "Google Drive", "WeChat attachment backup")
        store = KnowledgeStore(
            self.db_path,
            os.path.join(self.tmp.name, "obsidian"),
            attachment_archive_root=self.archive_root,
        )
        conn = store.connect()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def add_object(self, data, name):
        digest = hashlib.sha256(data).hexdigest()
        relpath = os.path.join("objects", "sha256", digest[:2], f"{digest}--{name}")
        path = os.path.join(self.archive_root, relpath)
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(path, "wb") as file:
            file.write(data)
        os.chmod(path, 0o600)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            INSERT INTO attachment_objects(sha256, size, object_relpath, original_name, created_at)
            VALUES (?, ?, ?, ?, 1)
            """,
            (digest, len(data), relpath, name),
        )
        conn.execute(
            """
            INSERT INTO attachment_mentions(
                event_id, topic_id, source_message_id, resource_index, kind,
                original_name, status, resolution_method, object_sha256,
                created_at, updated_at
            ) VALUES (?, ?, ?, 0, 'file', ?, 'original_archived',
                      'fixture_resolution', ?, 1, 1)
            """,
            (
                int(digest[:8], 16),
                int(digest[8:16], 16),
                "wgmsg_" + digest[:32],
                name,
                digest,
            ),
        )
        conn.commit()
        conn.close()
        return digest, path

    def backup(self, *, target=None, suffix="fixture"):
        return AttachmentBackup(
            self.db_path,
            self.archive_root,
            self.target if target is None else target,
            now_func=lambda: 1_780_000_000,
            id_factory=lambda: suffix,
        )

    def test_plan_is_read_only_and_provider_neutral(self):
        self.add_object(b"one", "private filename one.txt")
        self.add_object(b"two", "private filename two.txt")
        backup = self.backup()

        plan = backup.plan()

        self.assertEqual(plan["state"], "ready")
        self.assertEqual(plan["objects"], 2)
        self.assertEqual(plan["statuses"]["missing"], 2)
        self.assertFalse(os.path.exists(self.target))

    def test_run_copies_objects_then_writes_manifest_and_complete_marker(self):
        first, _ = self.add_object(b"one", "one.txt")
        second, _ = self.add_object(b"two", "two.txt")
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            INSERT INTO attachment_mentions(
                source_message_id, resource_index, kind, original_name,
                status, created_at, updated_at
            ) VALUES ('wgmsg_unresolved', 0, 'file', 'not-yet-resolved.txt',
                      'missing_retryable', 1, 1)
            """
        )
        conn.commit()
        conn.close()
        backup = self.backup()

        result = backup.run()

        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["statuses"]["target_copied"], 2)
        snapshot_dir = os.path.join(
            self.target,
            "v2",
            "snapshots",
            result["snapshot_id"],
        )
        manifest_path = os.path.join(snapshot_dir, "manifest.json")
        catalog_path = os.path.join(snapshot_dir, "catalog.json")
        complete_path = os.path.join(snapshot_dir, "COMPLETE")
        self.assertTrue(os.path.isfile(manifest_path))
        self.assertTrue(os.path.isfile(catalog_path))
        self.assertTrue(os.path.isfile(complete_path))
        with open(manifest_path, encoding="utf-8") as file:
            manifest = json.load(file)
        self.assertEqual(manifest["schema"], BACKUP_SCHEMA)
        self.assertEqual(
            {row["sha256"] for row in manifest["objects"]},
            {first, second},
        )
        serialized = json.dumps(manifest)
        self.assertNotIn("one.txt", serialized)
        self.assertNotIn("Google", serialized)
        self.assertNotIn("provider", serialized.lower())
        with open(catalog_path, encoding="utf-8") as file:
            catalog = json.load(file)
        self.assertEqual(catalog["entry_count"], 3)
        self.assertEqual(
            {entry["original_name"] for entry in catalog["entries"]},
            {"one.txt", "two.txt", "not-yet-resolved.txt"},
        )
        self.assertEqual({entry["kind"] for entry in catalog["entries"]}, {"file"})
        self.assertTrue(all(entry["source_message_id"].startswith("wgmsg_") for entry in catalog["entries"]))
        resolved = [entry for entry in catalog["entries"] if entry["object_sha256"]]
        unresolved = [entry for entry in catalog["entries"] if not entry["object_sha256"]]
        self.assertTrue(all(isinstance(entry["topic_id"], int) for entry in resolved))
        self.assertTrue(all(isinstance(entry["event_id"], int) for entry in resolved))
        self.assertEqual({entry["status"] for entry in resolved}, {"original_archived"})
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0]["object_sha256"], "")
        self.assertEqual(unresolved[0]["status"], "missing_retryable")
        self.assertIsNone(unresolved[0]["object_size"])
        catalog_text = json.dumps(catalog)
        self.assertNotIn("wxid", catalog_text.lower())
        self.assertNotIn("cache", catalog_text.lower())
        self.assertNotIn("raw chat body", catalog_text.lower())
        self.assertEqual(stat.S_IMODE(os.stat(manifest_path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(catalog_path).st_mode), 0o600)
        with open(complete_path, encoding="utf-8") as file:
            complete_marker = json.load(file)
        with open(manifest_path, "rb") as file:
            self.assertEqual(
                complete_marker["manifest_sha256"],
                hashlib.sha256(file.read()).hexdigest(),
            )
        with open(catalog_path, "rb") as file:
            self.assertEqual(
                complete_marker["catalog_sha256"],
                hashlib.sha256(file.read()).hexdigest(),
            )

    def test_complete_digest_and_catalog_consistency_are_required_across_restore_stages(self):
        digest, source_path = self.add_object(b"cross-stage", "cross-stage.bin")
        completed = self.backup(suffix="cross-stage").run()
        snapshot_dir = os.path.join(
            self.target,
            "v2",
            "snapshots",
            completed["snapshot_id"],
        )
        os.unlink(source_path)
        os.unlink(self.db_path)

        catalog_path = os.path.join(snapshot_dir, "catalog.json")
        with open(catalog_path, encoding="utf-8") as file:
            catalog = json.load(file)
        catalog["entries"][0]["object_size"] += 1
        with open(catalog_path, "w", encoding="utf-8") as file:
            json.dump(catalog, file)

        self.assertEqual(
            self.backup().restore_plan(completed["snapshot_id"])["state"],
            "snapshot_unavailable",
        )

        with open(catalog_path, "w", encoding="utf-8") as file:
            json.dump(
                {
                    "schema": BACKUP_SCHEMA,
                    "snapshot_id": completed["snapshot_id"],
                    "entry_count": 1,
                    "entries": [{
                        "object_sha256": digest,
                        "object_size": len(b"cross-stage") + 1,
                        "original_name": "cross-stage.bin",
                        "kind": "file",
                        "source_message_id": "wgmsg_fixture",
                        "topic_id": 1,
                        "event_id": 1,
                        "status": "original_archived",
                        "resolution_method": "fixture",
                    }],
                },
                file,
                sort_keys=True,
                separators=(",", ":"),
            )
            file.write("\n")
        complete_path = os.path.join(snapshot_dir, "COMPLETE")
        with open(complete_path, encoding="utf-8") as file:
            complete_marker = json.load(file)
        with open(catalog_path, "rb") as file:
            complete_marker["catalog_sha256"] = hashlib.sha256(file.read()).hexdigest()
        with open(complete_path, "w", encoding="utf-8") as file:
            json.dump(complete_marker, file)

        self.assertEqual(
            self.backup().verify(completed["snapshot_id"])["state"],
            "snapshot_unavailable",
        )

    def test_second_run_verifies_existing_target_without_duplicate_objects(self):
        digest, _ = self.add_object(b"immutable", "immutable.bin")
        first = self.backup(suffix="first").run()
        second = self.backup(suffix="second").run()

        self.assertEqual(first["statuses"]["target_copied"], 1)
        self.assertEqual(second["statuses"]["target_verified"], 1)
        object_path = os.path.join(
            self.target,
            "v2",
            "objects",
            "sha256",
            digest[:2],
            digest,
        )
        self.assertTrue(os.path.isfile(object_path))

    def test_verify_detects_target_corruption_and_failed_run_has_no_complete_snapshot(self):
        digest, _ = self.add_object(b"canonical", "canonical.bin")
        complete = self.backup(suffix="good").run()
        object_path = os.path.join(
            self.target,
            "v2",
            "objects",
            "sha256",
            digest[:2],
            digest,
        )
        with open(object_path, "wb") as file:
            file.write(b"corrupt")

        verified = self.backup(suffix="verify").verify(complete["snapshot_id"])
        failed = self.backup(suffix="failed").run()

        self.assertEqual(verified["state"], "target_failed")
        self.assertEqual(verified["target_failed"], 1)
        self.assertEqual(failed["state"], "target_failed")
        failed_snapshot = os.path.join(
            self.target,
            "v2",
            "snapshots",
            failed["snapshot_id"],
        )
        self.assertFalse(os.path.exists(os.path.join(failed_snapshot, "COMPLETE")))
        receipt = os.path.join(
            self.target,
            "v2",
            "receipts",
            failed["snapshot_id"] + ".json",
        )
        self.assertTrue(os.path.isfile(receipt))

    def test_source_missing_fails_without_publishing_complete_snapshot(self):
        _digest, source_path = self.add_object(b"will disappear", "missing.bin")
        os.unlink(source_path)

        result = self.backup().run()

        self.assertEqual(result["state"], "target_failed")
        self.assertEqual(result["statuses"]["target_failed"], 1)

    def test_restore_plan_is_read_only_and_counts_missing_local_objects(self):
        _digest, source_path = self.add_object(b"restore me", "restore.bin")
        completed = self.backup().run()
        os.unlink(source_path)

        plan = self.backup().restore_plan(completed["snapshot_id"])

        self.assertEqual(plan["state"], "ready")
        self.assertEqual(plan["restore_objects"], 1)
        self.assertEqual(plan["restore_bytes"], len(b"restore me"))
        self.assertFalse(os.path.exists(source_path))

    def test_restore_plan_uses_snapshot_catalog_when_local_db_is_absent(self):
        _digest, source_path = self.add_object(b"recover without local catalog", "recovery.txt")
        completed = self.backup().run()
        os.unlink(source_path)
        os.unlink(self.db_path)

        plan = self.backup().restore_plan(completed["snapshot_id"])

        self.assertEqual(plan["state"], "ready")
        self.assertEqual(plan["restore_objects"], 1)
        self.assertEqual(plan["catalog_entries"], 1)
        self.assertFalse(os.path.exists(self.db_path))

    def test_drive_only_cas_object_is_snapshotted_verified_and_restore_planned(self):
        db_dir = os.path.join(self.tmp.name, "xwechat_files", "fixture", "db_storage")
        file_root = os.path.join(os.path.dirname(db_dir), "msg", "file", "2026-05")
        os.makedirs(file_root, exist_ok=True)
        data = b"selected chat only bytes"
        source_path = os.path.join(file_root, "selected-only.txt")
        with open(source_path, "wb") as handle:
            handle.write(data)
        archive = AttachmentArchive(
            self.db_path,
            self.archive_root,
            db_dir=db_dir,
            min_free_bytes=0,
        )

        preserved = archive.preserve_file_mention({
            "kind": "file",
            "source_message_id": "wgmsg_selected_only",
            "resource_index": 0,
            "source_month": "2026-05",
            "original_name": "selected-only.txt",
            "declared_size": len(data),
            "declared_hash": hashlib.sha256(data).hexdigest(),
        })

        self.assertEqual(preserved["status"], "ready_local")
        self.assertEqual(archive.status()["objects"], 1)
        conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM attachment_objects").fetchone()[0], 0)
        finally:
            conn.close()
        backup = self.backup(suffix="drive-only")
        self.assertEqual(backup.plan()["objects"], 1)
        completed = backup.run()
        self.assertEqual(backup.verify(completed["snapshot_id"])["state"], "target_verified")
        os.unlink(os.path.join(self.archive_root, preserved["object_relpath"]))
        restore = backup.restore_plan(completed["snapshot_id"])
        self.assertEqual(restore["restore_objects"], 1)
        snapshot_dir = os.path.join(self.target, "v2", "snapshots", completed["snapshot_id"])
        with open(os.path.join(snapshot_dir, "catalog.json"), encoding="utf-8") as handle:
            catalog = json.load(handle)
        self.assertEqual(catalog["entries"][0]["source_message_id"], "wgmsg_selected_only")
        self.assertNotIn("fixture", json.dumps(catalog))

    def test_plan_and_run_reject_unsafe_target_boundaries(self):
        self.add_object(b"boundary", "boundary.bin")
        nested = os.path.join(self.archive_root, "backup")
        ancestor = self.tmp.name
        symlink = os.path.join(self.tmp.name, "archive-link")
        os.symlink(self.archive_root, symlink)
        cases = (
            (self.archive_root, "target_overlaps_local_source"),
            (nested, "target_overlaps_local_source"),
            (ancestor, "target_overlaps_local_source"),
            (os.path.abspath(os.sep), "target_is_filesystem_root"),
            (os.path.join(symlink, "escaped"), "target_overlaps_local_source"),
        )
        for target, error_code in cases:
            with self.subTest(target=target):
                backup = self.backup(target=target)
                plan = backup.plan()
                run = backup.run()
                self.assertEqual(plan["state"], "invalid_target")
                self.assertEqual(run["state"], "invalid_target")
                self.assertEqual(plan["error_code"], error_code)
                self.assertEqual(run["error_code"], error_code)

    def test_run_never_prunes_unmanaged_target_files(self):
        self.add_object(b"managed", "managed.bin")
        extra = os.path.join(self.target, "do-not-prune.txt")
        os.makedirs(self.target, exist_ok=True)
        with open(extra, "w", encoding="utf-8") as file:
            file.write("user-owned")

        self.backup().run()

        with open(extra, encoding="utf-8") as file:
            self.assertEqual(file.read(), "user-owned")

    def test_unconfigured_target_is_explicit(self):
        self.add_object(b"local only", "local.bin")
        backup = self.backup(target="")

        self.assertEqual(backup.plan()["state"], "target_not_configured")
        self.assertEqual(backup.run()["state"], "target_not_configured")
        self.assertEqual(backup.verify()["state"], "snapshot_unavailable")

    def test_missing_catalog_is_reported_without_writing_target(self):
        legacy_db = os.path.join(self.tmp.name, "legacy.db")
        conn = sqlite3.connect(legacy_db)
        conn.execute("CREATE TABLE topics(topic_id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        backup = AttachmentBackup(
            legacy_db,
            self.archive_root,
            self.target,
            now_func=lambda: 1_780_000_000,
            id_factory=lambda: "legacy",
        )

        self.assertEqual(backup.plan()["state"], "catalog_unavailable")
        self.assertEqual(backup.run()["state"], "catalog_unavailable")
        self.assertFalse(os.path.exists(self.target))


if __name__ == "__main__":
    unittest.main()
