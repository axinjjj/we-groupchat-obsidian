import unittest
import sys
from types import SimpleNamespace
from unittest.mock import patch

from core import keychain


class KeychainTests(unittest.TestCase):
    def test_new_service_name_is_primary_identity(self):
        self.assertEqual(keychain.SERVICE_NAME, "we-groupchat-obsidian")
        self.assertEqual(keychain.LEGACY_SERVICE_NAMES, ("wechat-summary",))

    def test_credential_store_label_is_platform_specific(self):
        with patch("core.keychain.sys.platform", "win32"):
            self.assertEqual(keychain.credential_store_label(), "Windows 凭据管理器")
        with patch("core.keychain.sys.platform", "darwin"):
            self.assertEqual(keychain.credential_store_label(), "macOS 钥匙串")

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

        with patch("core.keychain.sys.platform", "darwin"), \
             patch("core.keychain.subprocess.run", side_effect=fake_run):
            self.assertEqual(keychain.load_key("ai-api-key"), "legacy-key")

        self.assertEqual(calls[0][calls[0].index("-s") + 1], "we-groupchat-obsidian")
        self.assertEqual(calls[1][calls[1].index("-s") + 1], "wechat-summary")

    def test_windows_dispatches_to_credential_manager_backend(self):
        with patch("core.keychain.sys.platform", "win32"), \
             patch("core.keychain._windows_load_key", return_value="windows-key") as load:
            self.assertEqual(keychain.load_key("ai-api-key"), "windows-key")

        load.assert_called_once_with("ai-api-key")

    def test_missing_windows_credential_is_not_an_error(self):
        class CredentialError(Exception):
            pass

        fake_cred = SimpleNamespace(
            CRED_TYPE_GENERIC=1,
            CredRead=lambda *args: (_ for _ in ()).throw(
                CredentialError(1168, "CredRead", "not found")
            ),
        )
        with patch.dict(
            sys.modules,
            {
                "pywintypes": SimpleNamespace(error=CredentialError),
                "win32cred": fake_cred,
            },
        ):
            self.assertIsNone(keychain._windows_load_key("missing-account"))

    def test_windows_credential_blob_uses_unicode_api_contract(self):
        class CredentialError(Exception):
            pass

        written = []
        fake_cred = SimpleNamespace(
            CRED_TYPE_GENERIC=1,
            CRED_PERSIST_LOCAL_MACHINE=2,
            CredWrite=lambda value, _flags: written.append(value),
        )
        with patch.dict(
            sys.modules,
            {
                "pywintypes": SimpleNamespace(error=CredentialError),
                "win32cred": fake_cred,
            },
        ):
            self.assertTrue(keychain._windows_save_key("account", "密钥"))

        self.assertEqual(written[0]["CredentialBlob"], "密钥")

    def test_windows_unicode_credential_blob_decodes_utf16(self):
        class CredentialError(Exception):
            pass

        value = "ab" * 32
        fake_cred = SimpleNamespace(
            CRED_TYPE_GENERIC=1,
            CredRead=lambda *_args: {
                "CredentialBlob": value.encode("utf-16-le"),
            },
        )
        with patch.dict(
            sys.modules,
            {
                "pywintypes": SimpleNamespace(error=CredentialError),
                "win32cred": fake_cred,
            },
        ):
            self.assertEqual(keychain._windows_load_key("account"), value)


if __name__ == "__main__":
    unittest.main()
