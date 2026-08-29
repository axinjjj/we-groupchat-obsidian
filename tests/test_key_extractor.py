import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from core.key_extractor import (
    _parse_raw_keys_from_log,
    _parse_raw_keys_from_text,
    _rematch_keys_from_output,
    process_lookup_available,
)


class KeyExtractorTests(unittest.TestCase):
    def test_process_lookup_availability_uses_current_process_as_visible_sentinel(self):
        result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=f"{os.getpid()}\n",
            stderr="",
        )
        with patch("core.key_extractor.sys.platform", "darwin"), \
             patch("core.key_extractor.subprocess.run", return_value=result) as run:
            self.assertTrue(process_lookup_available())

        run.assert_called_once_with(
            ["ps", "-p", str(os.getpid()), "-o", "pid="],
            capture_output=True,
            text=True,
        )

    def test_missing_extract_log_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "missing.log")

            self.assertEqual(_parse_raw_keys_from_log(missing), [])

    def test_parse_raw_keys_from_scanner_text(self):
        key_hex = "a" * 64
        salt_hex = "12" * 16
        text = f"message/message_0.db   {key_hex}   {salt_hex}\n"

        self.assertEqual(_parse_raw_keys_from_text(text), [(key_hex, salt_hex)])

    def test_rematch_keys_from_in_memory_output_without_extract_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = os.path.join(tmp, "db_storage")
            os.makedirs(os.path.join(db_dir, "message"))
            keys_file = os.path.join(tmp, "all_keys.json")
            extract_log = os.path.join(tmp, "extract_keys.log")
            key_hex = "b" * 64
            salt_hex = "34" * 16
            db_path = os.path.join(db_dir, "message", "message_0.db")
            with open(db_path, "wb") as handle:
                handle.write(bytes.fromhex(salt_hex))
                handle.write(b"encrypted-body")

            with patch("core.key_extractor.KEYS_FILE", keys_file), \
                 patch("core.key_extractor.EXTRACT_LOG", extract_log):
                matched = _rematch_keys_from_output(
                    db_dir,
                    f"(unknown) {key_hex} {salt_hex}\n",
                )

            self.assertEqual(matched, {"message/message_0.db": {"enc_key": key_hex}})
            self.assertFalse(os.path.exists(extract_log))
            with open(keys_file, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), matched)


if __name__ == "__main__":
    unittest.main()
