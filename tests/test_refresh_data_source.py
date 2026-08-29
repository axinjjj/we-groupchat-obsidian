import sys
import unittest
from unittest.mock import patch

from scripts.refresh_data_source import RefreshResult, main, refresh_data_source


class RefreshDataSourceTests(unittest.TestCase):
    def test_reports_success_when_all_database_keys_are_present(self):
        config = {"db_dir": "/tmp/db_storage"}
        keys = {"message/msg_0.db": {"enc_key": "abc"}}

        with patch("scripts.refresh_data_source.load_config", return_value=config), \
             patch("scripts.refresh_data_source.process_lookup_available", return_value=True), \
             patch("scripts.refresh_data_source.is_wechat_running", return_value=True), \
             patch("scripts.refresh_data_source.os.path.isdir", return_value=True), \
             patch("scripts.refresh_data_source.extract_keys", return_value=keys) as extract, \
             patch("scripts.refresh_data_source.check_new_databases", return_value=[]):
            result = refresh_data_source(raw_key_hex="ab" * 32 if sys.platform == "win32" else None)

        self.assertTrue(result.ok)
        self.assertEqual(result.key_count, 1)
        self.assertEqual(result.missing_databases, [])
        extract.assert_called_once_with(
            raw_key_hex="ab" * 32 if sys.platform == "win32" else None
        )

    def test_reports_missing_database_keys_after_refresh(self):
        config = {"db_dir": "/tmp/db_storage"}
        keys = {"message/msg_0.db": {"enc_key": "abc"}}

        with patch("scripts.refresh_data_source.load_config", return_value=config), \
             patch("scripts.refresh_data_source.process_lookup_available", return_value=True), \
             patch("scripts.refresh_data_source.is_wechat_running", return_value=True), \
             patch("scripts.refresh_data_source.os.path.isdir", return_value=True), \
             patch("scripts.refresh_data_source.extract_keys", return_value=keys), \
             patch("scripts.refresh_data_source.check_new_databases", return_value=["message/weclaw.db"]):
            result = refresh_data_source(raw_key_hex="ab" * 32 if sys.platform == "win32" else None)

        self.assertFalse(result.ok)
        self.assertEqual(result.key_count, 1)
        self.assertEqual(result.missing_databases, ["message/weclaw.db"])

    def test_reports_when_process_list_cannot_be_checked(self):
        with patch("scripts.refresh_data_source.sys.platform", "darwin"), \
             patch("scripts.refresh_data_source.process_lookup_available", return_value=False):
            result = refresh_data_source()

        self.assertFalse(result.ok)
        self.assertIn("无法检测", result.message)

    def test_windows_requires_explicit_private_raw_key_input(self):
        with patch("scripts.refresh_data_source.sys.platform", "win32"):
            result = refresh_data_source()

        self.assertFalse(result.ok)
        self.assertIn("--raw-key", result.message)

    def test_remembered_raw_key_is_loaded_without_command_line_secret(self):
        with patch("scripts.refresh_data_source.sys.platform", "win32"), \
             patch("scripts.refresh_data_source.load_key", return_value="ab" * 32) as load, \
             patch(
                 "scripts.refresh_data_source.refresh_data_source",
                 return_value=RefreshResult(ok=True, key_count=2, missing_databases=[]),
             ) as refresh:
            result = main(["--stored-raw-key"])

        self.assertEqual(result, 0)
        load.assert_called_once()
        refresh.assert_called_once_with(raw_key_hex="ab" * 32)

    def test_prompted_raw_key_is_remembered_only_after_full_verification(self):
        with patch("scripts.refresh_data_source.sys.platform", "win32"), \
             patch("scripts.refresh_data_source.getpass.getpass", return_value="cd" * 32), \
             patch(
                 "scripts.refresh_data_source.refresh_data_source",
                 return_value=RefreshResult(ok=True, key_count=2, missing_databases=[]),
             ), \
             patch("scripts.refresh_data_source.save_key", return_value=True) as save:
            result = main(["--raw-key", "--remember-raw-key"])

        self.assertEqual(result, 0)
        save.assert_called_once_with("windows-weixin-raw-key", "cd" * 32)


if __name__ == "__main__":
    unittest.main()
