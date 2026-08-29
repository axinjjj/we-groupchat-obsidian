import unittest
from unittest.mock import patch

from core.windows_runtime import inspect_windows_runtime


class WindowsRuntimeTests(unittest.TestCase):
    def test_ready_requires_database_verified_keys_and_no_missing_required_key(self):
        with patch("core.windows_runtime.load_config", return_value={"db_dir": "db"}), \
             patch("core.windows_runtime.os.path.isdir", return_value=True), \
             patch("core.windows_runtime.get_cached_keys", return_value={"contact/contact.db": {"enc_key": "x"}}), \
             patch("core.windows_runtime.load_key", return_value="raw"), \
             patch("core.windows_runtime.check_new_databases", return_value=[]), \
             patch("core.windows_runtime.get_weixin_app_path", return_value="Weixin.exe"), \
             patch("core.windows_runtime.process_lookup_available", return_value=True), \
             patch("core.windows_runtime.is_weixin_running", return_value=True):
            status = inspect_windows_runtime()

        self.assertTrue(status["ready"])
        self.assertEqual(status["key_count"], 1)
        self.assertTrue(status["raw_key_remembered"])

    def test_missing_required_key_is_not_ready(self):
        with patch("core.windows_runtime.load_config", return_value={"db_dir": "db"}), \
             patch("core.windows_runtime.os.path.isdir", return_value=True), \
             patch("core.windows_runtime.get_cached_keys", return_value={"contact/contact.db": {"enc_key": "x"}}), \
             patch("core.windows_runtime.load_key", return_value=None), \
             patch("core.windows_runtime.check_new_databases", return_value=["session/session.db"]), \
             patch("core.windows_runtime.get_weixin_app_path", return_value="Weixin.exe"), \
             patch("core.windows_runtime.process_lookup_available", return_value=True), \
             patch("core.windows_runtime.is_weixin_running", return_value=True):
            status = inspect_windows_runtime()

        self.assertFalse(status["ready"])
        self.assertEqual(status["missing_required_key_count"], 1)

    def test_empty_database_setting_does_not_treat_checkout_as_source(self):
        with patch("core.windows_runtime.load_config", return_value={"db_dir": ""}), \
             patch("core.windows_runtime.get_cached_keys", return_value=None), \
             patch("core.windows_runtime.load_key", return_value=None), \
             patch("core.windows_runtime.get_weixin_app_path", return_value=None), \
             patch("core.windows_runtime.process_lookup_available", return_value=False):
            status = inspect_windows_runtime()

        self.assertFalse(status["db_configured"])
        self.assertFalse(status["db_available"])
        self.assertFalse(status["ready"])


if __name__ == "__main__":
    unittest.main()
