import unittest
from unittest.mock import patch

from app import WeGroupchatObsidianApp


class AppWindowsTests(unittest.TestCase):
    def test_remembered_raw_key_renews_missing_page_keys(self):
        app = WeGroupchatObsidianApp.__new__(WeGroupchatObsidianApp)
        app.config = {"db_dir": "db_storage"}
        existing = {"contact/contact.db": {"enc_key": "old"}}
        refreshed = {
            "contact/contact.db": {"enc_key": "new"},
            "session/session.db": {"enc_key": "new"},
        }
        with patch("app.sys.platform", "win32"), \
             patch("app.os.path.isdir", return_value=True), \
             patch("app.check_new_databases", return_value=["session/session.db"]), \
             patch("app.load_key", return_value="ab" * 32), \
             patch("app.extract_keys", return_value=refreshed) as extract:
            result = app._refresh_windows_keys_from_credential(existing)

        self.assertEqual(result, refreshed)
        extract.assert_called_once_with(raw_key_hex="ab" * 32)

    def test_no_remembered_raw_key_keeps_existing_page_keys(self):
        app = WeGroupchatObsidianApp.__new__(WeGroupchatObsidianApp)
        app.config = {"db_dir": "db_storage"}
        existing = {"contact/contact.db": {"enc_key": "old"}}
        with patch("app.sys.platform", "win32"), \
             patch("app.os.path.isdir", return_value=True), \
             patch("app.check_new_databases", return_value=["session/session.db"]), \
             patch("app.load_key", return_value=None), \
             patch("app.extract_keys") as extract:
            result = app._refresh_windows_keys_from_credential(existing)

        self.assertIs(result, existing)
        extract.assert_not_called()


if __name__ == "__main__":
    unittest.main()
