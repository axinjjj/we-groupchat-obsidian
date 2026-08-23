import os
import tempfile
import unittest
from urllib.parse import quote

from core.notification_target import (
    notification_data_for_path,
    notification_open_commands_for_path,
    target_path_from_notification,
)


class NotificationTargetTests(unittest.TestCase):
    def test_notification_data_for_path_expands_to_absolute_path(self):
        data = notification_data_for_path("~/note.md")

        self.assertEqual(data["open_path"], os.path.expanduser("~/note.md"))

    def test_target_path_from_notification_returns_existing_path_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "note.md")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("# note\n")

            self.assertEqual(target_path_from_notification({"open_path": path}), path)
            self.assertEqual(target_path_from_notification({"open_path": os.path.join(tmp, "missing.md")}), "")
            self.assertEqual(target_path_from_notification(None), "")

    def test_markdown_notification_targets_open_with_obsidian_uri_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "My Vault", "微信群聊", "today #1.md")
            os.makedirs(os.path.dirname(path))
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("# note\n")

            commands = notification_open_commands_for_path(path)

        expected_uri = "obsidian://open?path=" + quote(path, safe="/")
        self.assertEqual(commands[0], ["open", expected_uri])
        self.assertEqual(commands[1], ["open", path])

    def test_non_markdown_notification_targets_keep_default_open(self):
        commands = notification_open_commands_for_path("/tmp/export.txt")

        self.assertEqual(commands, [["open", "/tmp/export.txt"]])


if __name__ == "__main__":
    unittest.main()
