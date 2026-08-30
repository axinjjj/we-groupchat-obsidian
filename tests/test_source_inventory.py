import json
import multiprocessing
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from core.source_inventory import SourceInventoryError, SourceInventoryStore
from core.wechat_db import WeChatDB, WeChatSourceDegraded


def _sqlite_file(path, table="messages"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(f"CREATE TABLE [{table}](value TEXT)")
        conn.commit()
    finally:
        conn.close()


def _inventory_reconcile_worker(path, namespace, relative_path, start, result):
    start.wait(10)
    try:
        snapshot = SourceInventoryStore(path).reconcile(
            namespace,
            [{
                "relative_path": relative_path,
                "generation_id": relative_path,
                "state": "present",
            }],
        )
        result.put(("ok", snapshot.inventory_revision))
    except Exception as exc:
        result.put((type(exc).__name__, str(exc)))


class SourceInventoryStoreTests(unittest.TestCase):
    def test_concurrent_reconcile_preserves_inventory_union_and_revisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "source_inventory.json")
            namespace = "concurrent-source"
            context = multiprocessing.get_context("spawn")
            start = context.Event()
            result = context.Queue()
            processes = [
                context.Process(
                    target=_inventory_reconcile_worker,
                    args=(path, namespace, relative_path, start, result),
                )
                for relative_path in (
                    "message/message_1.db",
                    "message/message_2.db",
                )
            ]
            for process in processes:
                process.start()
            start.set()
            outcomes = [result.get(timeout=10) for _ in processes]
            for process in processes:
                process.join(10)
                self.assertEqual(process.exitcode, 0)

            self.assertEqual([outcome[0] for outcome in outcomes], ["ok", "ok"])
            snapshot = SourceInventoryStore(path).inspect(namespace)
            self.assertEqual(snapshot.inventory_revision, 2)
            self.assertEqual(
                {shard["relative_path"] for shard in snapshot.shards},
                {"message/message_1.db", "message/message_2.db"},
            )

    def test_wechat_db_import_does_not_require_posix_lock_module(self):
        script = """
import builtins
original_import = builtins.__import__

def guarded_import(name, *args, **kwargs):
    if name == "fcntl":
        raise ModuleNotFoundError("fcntl intentionally unavailable")
    return original_import(name, *args, **kwargs)

builtins.__import__ = guarded_import
import core.wechat_db
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            capture_output=True,
            text=True,
            timeout=20,
        )

        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_inspect_absent_inventory_is_read_only(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "private", "source_inventory.json")
            snapshot = SourceInventoryStore(path).inspect("source-a")

            self.assertFalse(snapshot.complete)
            self.assertIn("source_inventory_uninitialized", snapshot.error_codes)
            self.assertFalse(os.path.exists(os.path.dirname(path)))
            self.assertFalse(os.path.lexists(path + ".lock"))

    def test_inventory_rejects_nonregular_storage_path(self):
        with tempfile.TemporaryDirectory() as root:
            target = os.path.join(root, "target.json")
            link = os.path.join(root, "source_inventory.json")
            with open(target, "w", encoding="utf-8") as handle:
                json.dump({
                    "schema": "we-groupchat-obsidian.source-inventory.v1",
                    "revision": 0,
                    "sources": {},
                }, handle)
            os.symlink(target, link)

            with self.assertRaisesRegex(SourceInventoryError, "source_inventory_corrupt"):
                SourceInventoryStore(link).inspect("source-a")


class WeChatSourceInventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cache_dir = WeChatDB.CACHE_DIR
        WeChatDB.CACHE_DIR = os.path.join(self.tmp.name, "cache")
        self.source_root = os.path.join(self.tmp.name, "source")
        self.inventory_path = os.path.join(self.tmp.name, "state", "inventory.json")

    def tearDown(self):
        WeChatDB.CACHE_DIR = self.old_cache_dir
        self.tmp.cleanup()

    def _db(self, keys=None):
        os.makedirs(self.source_root, exist_ok=True)
        return WeChatDB(
            self.source_root,
            keys or {},
            source_inventory_store=SourceInventoryStore(self.inventory_path),
        )

    def test_missing_expected_shard_is_incomplete_but_present_shard_remains_visible(self):
        first_rel = "message/message_0.db"
        second_rel = "message/message_1.db"
        first_path = os.path.join(self.source_root, first_rel)
        second_path = os.path.join(self.source_root, second_rel)
        _sqlite_file(first_path, "first")
        _sqlite_file(second_path, "second")
        db = self._db()

        initial = db.get_source_inventory()
        initial_generations = set(initial["present_generation_ids"])
        parked = os.path.join(self.tmp.name, "parked-message-1.db")
        os.rename(second_path, parked)
        degraded = db.get_source_inventory()
        missing_generation = next(
            iter(initial_generations - set(degraded["present_generation_ids"]))
        )

        self.assertFalse(degraded["complete"])
        self.assertEqual(degraded["counts"]["missing_file"], 1)
        self.assertEqual(degraded["counts"]["present"], 1)
        self.assertEqual(len(degraded["present_generation_ids"]), 1)
        with self.assertRaisesRegex(WeChatSourceDegraded, "source_inventory_incomplete"):
            db.get_message_shards("chat@chatroom")

        os.rename(parked, second_path)
        recovered = db.get_source_inventory()
        self.assertTrue(recovered["complete"])
        self.assertEqual(recovered["counts"]["present"], 2)
        self.assertIn(missing_generation, recovered["present_generation_ids"])

    def test_inventory_union_includes_key_only_and_newly_discovered_shards(self):
        missing_rel = "message/message_7.db"
        db = self._db({missing_rel: {"enc_key": "11" * 32}})

        missing = db.get_source_inventory()
        self.assertFalse(missing["complete"])
        self.assertEqual(missing["counts"]["missing_file"], 1)

        live_rel = "message/biz_message_2.db"
        _sqlite_file(os.path.join(self.source_root, live_rel), "live")
        discovered = db.get_source_inventory()
        self.assertEqual(discovered["counts"]["missing_file"], 1)
        self.assertEqual(discovered["counts"]["present"], 1)
        self.assertEqual(len(discovered["shards"]), 2)

    def test_encrypted_shard_without_key_is_key_missing(self):
        rel_path = "message/message_3.db"
        path = os.path.join(self.source_root, rel_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"encrypted source fixture")

        snapshot = self._db().get_source_inventory()

        self.assertFalse(snapshot["complete"])
        self.assertEqual(snapshot["counts"]["key_missing"], 1)
        self.assertIn("source_key_missing", snapshot["error_codes"])

    def test_cache_only_shard_is_not_accepted_as_complete_source(self):
        db = self._db()
        rel_path = "message/message_4.db"
        _sqlite_file(db._cache_path(rel_path), "cached")

        snapshot = db.get_source_inventory()

        self.assertFalse(snapshot["complete"])
        self.assertEqual(snapshot["counts"]["cache_only"], 1)
        self.assertEqual(snapshot["present_generation_ids"], [])
        with self.assertRaisesRegex(WeChatSourceDegraded, "source_cache_only"):
            db.get_message_shards("chat@chatroom")

    def test_replaced_shard_has_new_generation_without_losing_logical_identity(self):
        rel_path = "message/message_5.db"
        source_path = os.path.join(self.source_root, rel_path)
        _sqlite_file(source_path, "first")
        db = self._db()
        first = db.get_source_inventory()

        replacement = os.path.join(self.tmp.name, "replacement.db")
        _sqlite_file(replacement, "second")
        os.replace(replacement, source_path)
        second = db.get_source_inventory()

        self.assertTrue(second["complete"])
        self.assertEqual(second["counts"]["generation_changed"], 1)
        self.assertEqual(
            first["shards"][0]["logical_shard_id"],
            second["shards"][0]["logical_shard_id"],
        )
        self.assertNotEqual(
            first["shards"][0]["generation_id"],
            second["shards"][0]["generation_id"],
        )

    def test_public_inventory_evidence_contains_no_source_path(self):
        rel_path = "message/message_6.db"
        _sqlite_file(os.path.join(self.source_root, rel_path), "visible")

        public = self._db().get_source_inventory()
        encoded = json.dumps(public, ensure_ascii=False)

        self.assertNotIn(self.tmp.name, encoded)
        self.assertNotIn(rel_path, encoded)
        self.assertTrue(public["source_namespace"])
        self.assertTrue(public["inventory_digest"])


if __name__ == "__main__":
    unittest.main()
