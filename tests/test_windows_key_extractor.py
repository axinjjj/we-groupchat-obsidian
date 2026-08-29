import hashlib
import hmac
import json
import os
import struct
import tempfile
import unittest
from unittest.mock import patch

from Crypto.Cipher import AES

from core.decryptor import (
    HMAC_SZ,
    PAGE_SZ,
    RESERVE_SZ,
    SQLITE_HDR,
    decrypt_database,
    derive_mac_key,
)
from core.windows_key_extractor import (
    build_key_map_from_raw_key,
    derive_page_key,
    extract_keys_from_raw_key,
    get_weixin_app_path,
)


def _encrypted_page1(page_key, salt):
    page = bytearray(hashlib.sha512(b"page-fixture").digest() * 64)
    page[:16] = salt
    mac_key = derive_mac_key(page_key, salt)
    signed = page[16 : PAGE_SZ - RESERVE_SZ + 16]
    digest = hmac.new(mac_key, signed, hashlib.sha512)
    digest.update(struct.pack("<I", 1))
    page[PAGE_SZ - HMAC_SZ :] = digest.digest()
    return bytes(page)


class WindowsKeyExtractorTests(unittest.TestCase):
    def test_weixin_install_detection_does_not_accept_classic_wechat(self):
        with patch("core.windows_key_extractor.os.path.isfile") as isfile:
            isfile.side_effect = lambda path: path.endswith(
                os.path.join("Tencent", "Weixin", "Weixin.exe")
            )

            path = get_weixin_app_path({"ProgramFiles": r"C:\Program Files"})

        self.assertEqual(
            path,
            os.path.join(r"C:\Program Files", "Tencent", "Weixin", "Weixin.exe"),
        )

    def test_raw_key_derives_existing_page_key_map_shape(self):
        raw_key = bytes(range(32))
        salt = bytes(range(16, 32))
        page_key = derive_page_key(raw_key, salt)
        with tempfile.TemporaryDirectory() as tmp:
            message_dir = os.path.join(tmp, "message")
            os.makedirs(message_dir)
            db_path = os.path.join(message_dir, "message_0.db")
            with open(db_path, "wb") as handle:
                handle.write(_encrypted_page1(page_key, salt))

            keys = build_key_map_from_raw_key(raw_key.hex(), tmp)

        self.assertEqual(
            keys,
            {"message/message_0.db": {"enc_key": page_key.hex()}},
        )

    def test_wrong_raw_key_is_not_persistable_as_page_key(self):
        raw_key = bytes(range(32))
        salt = bytes(range(16, 32))
        page_key = derive_page_key(raw_key, salt)
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "contact.db"), "wb") as handle:
                handle.write(_encrypted_page1(page_key, salt))

            keys = build_key_map_from_raw_key((b"x" * 32).hex(), tmp)

        self.assertEqual(keys, {})

    def test_refresh_preserves_existing_page_keys(self):
        raw_key = bytes(range(32))
        salt = bytes(range(16, 32))
        page_key = derive_page_key(raw_key, salt)
        existing_key = "ab" * 32
        with tempfile.TemporaryDirectory() as tmp:
            message_dir = os.path.join(tmp, "message")
            os.makedirs(message_dir)
            with open(os.path.join(message_dir, "message_0.db"), "wb") as handle:
                handle.write(_encrypted_page1(page_key, salt))
            keys_path = os.path.join(tmp, "all_keys.json")
            with open(keys_path, "w", encoding="utf-8") as handle:
                json.dump(
                    {"contact/contact.db": {"enc_key": existing_key}},
                    handle,
                )

            keys = extract_keys_from_raw_key(raw_key.hex(), tmp, keys_path)
            with open(keys_path, encoding="utf-8") as handle:
                persisted = json.load(handle)

        self.assertEqual(keys["contact/contact.db"]["enc_key"], existing_key)
        self.assertEqual(
            keys["message/message_0.db"]["enc_key"],
            page_key.hex(),
        )
        self.assertEqual(persisted, keys)

    def test_wrong_raw_key_leaves_existing_key_file_unchanged(self):
        raw_key = bytes(range(32))
        salt = bytes(range(16, 32))
        page_key = derive_page_key(raw_key, salt)
        original = '{"contact/contact.db":{"enc_key":"' + ("ab" * 32) + '"}}'
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "contact.db"), "wb") as handle:
                handle.write(_encrypted_page1(page_key, salt))
            keys_path = os.path.join(tmp, "all_keys.json")
            with open(keys_path, "w", encoding="utf-8") as handle:
                handle.write(original)

            keys = extract_keys_from_raw_key((b"x" * 32).hex(), tmp, keys_path)
            with open(keys_path, encoding="utf-8") as handle:
                after = handle.read()

        self.assertIsNone(keys)
        self.assertEqual(after, original)

    def test_raw_key_map_drives_original_database_decryptor(self):
        raw_key = bytes(range(32))
        salt = bytes(range(16))
        page_key = derive_page_key(raw_key, salt)
        iv = bytes(range(32, 48))
        body = bytes(index % 251 for index in range(4000))
        encrypted = AES.new(page_key, AES.MODE_CBC, iv).encrypt(body)
        page1 = bytearray(salt + encrypted + iv + bytes(HMAC_SZ))
        mac_key = derive_mac_key(page_key, salt)
        digest = hmac.new(
            mac_key,
            page1[16 : PAGE_SZ - RESERVE_SZ + 16],
            hashlib.sha512,
        )
        digest.update(struct.pack("<I", 1))
        page1[PAGE_SZ - HMAC_SZ :] = digest.digest()

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "session.db")
            output = os.path.join(tmp, "plain", "session.db")
            with open(db_path, "wb") as handle:
                handle.write(page1)
            keys = build_key_map_from_raw_key(raw_key.hex(), tmp)
            pages = decrypt_database(
                db_path,
                output,
                keys["session.db"]["enc_key"],
            )
            with open(output, "rb") as handle:
                plaintext = handle.read()

        self.assertEqual(pages, 1)
        self.assertEqual(plaintext[:16], SQLITE_HDR)
        self.assertEqual(plaintext[16:4016], body)


if __name__ == "__main__":
    unittest.main()
