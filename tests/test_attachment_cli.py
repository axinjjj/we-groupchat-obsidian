import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from core.knowledge import KnowledgeStore
from scripts import attachment_archive as archive_cli
from scripts import attachment_backup as backup_cli


class AttachmentCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "knowledge.db")
        self.obsidian = os.path.join(self.tmp.name, "obsidian")
        self.archive = os.path.join(self.tmp.name, "archive")
        self.target = os.path.join(self.tmp.name, "backup-target")
        self.db_dir = os.path.join(self.tmp.name, "xwechat_files", "wxid", "db_storage")
        os.makedirs(self.db_dir)
        self.config = {
            "monitor_knowledge_db": self.db_path,
            "monitor_obsidian_root": self.obsidian,
            "monitor_obsidian_subdir": "关注推送",
            "monitor_chat_display_name": "Fixture",
            "monitor_chat_username": "room@chatroom",
            "attachment_archive_root": self.archive,
            "attachment_archive_kinds": ["file"],
            "attachment_backup_target": self.target,
            "db_dir": self.db_dir,
        }
        self.store = KnowledgeStore(
            self.db_path,
            self.obsidian,
            "关注推送",
            attachment_archive_root=self.archive,
        )
        conn = self.store.connect()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def output_json(call):
        output = io.StringIO()
        with redirect_stdout(output):
            code = call()
        return code, json.loads(output.getvalue())

    def test_archive_status_is_read_only(self):
        with patch.object(archive_cli, "load_config", return_value=self.config):
            code, result = self.output_json(lambda: archive_cli.main(["status"]))
        self.assertEqual(code, 0)
        self.assertEqual(result["objects"], 0)
        self.assertFalse(os.path.exists(self.archive))

    def test_archive_backfill_defaults_to_plan_without_writes(self):
        result = self.store.apply_event(
            {
                "title": "Historical file",
                "summary": "Fixture",
                "topic_key": "historical-file",
                "category": "技术方法",
                "entities": [],
                "key_facts": [],
                "links": [],
                "event_type": "resource",
                "status_hint": "tracking",
            },
            [{
                "timestamp": 1,
                "time_str": "2026-05-29 03:16",
                "sender": "Fixture",
                "text": "[文件] historical.txt",
            }],
            self.config,
            {"relation": "new"},
        )
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM attachment_mentions WHERE event_id = ?", (result["event_id"],))
        conn.commit()
        conn.close()

        with patch.object(archive_cli, "load_config", return_value=self.config):
            code, output = self.output_json(lambda: archive_cli.main(["backfill"]))

        self.assertEqual(code, 0)
        self.assertEqual(output["mode"], "plan")
        self.assertEqual(output["plan"]["new_mentions"], 1)
        conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM attachment_mentions").fetchone()[0], 0)
        finally:
            conn.close()

    def test_backup_plan_does_not_create_target(self):
        with patch.object(backup_cli, "load_config", return_value=self.config):
            code, result = self.output_json(lambda: backup_cli.main(["plan"]))
        self.assertEqual(code, 0)
        self.assertEqual(result["state"], "ready")
        self.assertFalse(os.path.exists(self.target))

    def test_backup_target_configuration_does_not_echo_private_path(self):
        config = dict(self.config)
        with patch.object(backup_cli, "load_config", return_value=config), patch.object(
            backup_cli,
            "update_config",
        ) as update:
            code, result = self.output_json(
                lambda: backup_cli.main(["set-target", self.target])
            )

        self.assertEqual(code, 0)
        self.assertEqual(result, {"state": "configured", "target": "configured"})
        self.assertNotIn(self.target, json.dumps(result))
        update.assert_called_once_with(
            patch={"attachment_backup_target": self.target}
        )


if __name__ == "__main__":
    unittest.main()
