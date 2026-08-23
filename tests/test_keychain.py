import unittest
from unittest.mock import patch

from core import keychain


class KeychainTests(unittest.TestCase):
    def test_new_service_name_is_primary_identity(self):
        self.assertEqual(keychain.SERVICE_NAME, "we-groupchat-obsidian")
        self.assertEqual(keychain.LEGACY_SERVICE_NAMES, ("wechat-summary",))

    def test_load_key_falls_back_to_legacy_service(self):
        calls = []

        def fake_run(args, capture_output=True, text=True, check=True, timeout=5):
            calls.append(args)
            service = args[args.index("-s") + 1]
            if service == "we-groupchat-obsidian":
                raise keychain.subprocess.CalledProcessError(44, args)

            class Result:
                stdout = "legacy-key\n"

            return Result()

        with patch("core.keychain.subprocess.run", side_effect=fake_run):
            self.assertEqual(keychain.load_key("ai-api-key"), "legacy-key")

        self.assertEqual(calls[0][calls[0].index("-s") + 1], "we-groupchat-obsidian")
        self.assertEqual(calls[1][calls[1].index("-s") + 1], "wechat-summary")


if __name__ == "__main__":
    unittest.main()
