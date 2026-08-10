import unittest
from unittest.mock import patch

from scripts.refresh_data_source import refresh_data_source


class RefreshDataSourceTests(unittest.TestCase):
    def test_reports_success_when_all_database_keys_are_present(self):
        config = {"db_dir": "/tmp/db_storage"}
        keys = {"message/msg_0.db": {"enc_key": "abc"}}

        with patch("scripts.refresh_data_source.load_config", return_value=config), \
             patch("scripts.refresh_data_source.process_lookup_available", return_value=True), \
             patch("scripts.refresh_data_source.is_wechat_running", return_value=True), \
             patch("scripts.refresh_data_source.os.path.isdir", return_value=True), \
             patch("scripts.refresh_data_source.extract_keys", return_value=keys), \
             patch("scripts.refresh_data_source.check_new_databases", return_value=[]):
            result = refresh_data_source()

        self.assertTrue(result.ok)
        self.assertEqual(result.key_count, 1)
        self.assertEqual(result.missing_databases, [])

    def test_reports_missing_database_keys_after_refresh(self):
        config = {"db_dir": "/tmp/db_storage"}
        keys = {"message/msg_0.db": {"enc_key": "abc"}}

        with patch("scripts.refresh_data_source.load_config", return_value=config), \
             patch("scripts.refresh_data_source.process_lookup_available", return_value=True), \
             patch("scripts.refresh_data_source.is_wechat_running", return_value=True), \
             patch("scripts.refresh_data_source.os.path.isdir", return_value=True), \
             patch("scripts.refresh_data_source.extract_keys", return_value=keys), \
             patch("scripts.refresh_data_source.check_new_databases", return_value=["message/weclaw.db"]):
            result = refresh_data_source()

        self.assertFalse(result.ok)
        self.assertEqual(result.key_count, 1)
        self.assertEqual(result.missing_databases, ["message/weclaw.db"])

    def test_reports_when_process_list_cannot_be_checked(self):
        with patch("scripts.refresh_data_source.process_lookup_available", return_value=False):
            result = refresh_data_source()

        self.assertFalse(result.ok)
        self.assertIn("无法检测", result.message)


if __name__ == "__main__":
    unittest.main()
