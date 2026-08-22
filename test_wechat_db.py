import hashlib
import json
import os
import sqlite3
import struct
import tempfile
import unittest

from Crypto.Cipher import AES

from core.image_decoder import decode_wechat_image_data, detect_mime
from core.wechat_db import WeChatDB


class WeChatDBCacheRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cache_dir = WeChatDB.CACHE_DIR
        WeChatDB.CACHE_DIR = os.path.join(self.tmp.name, "cache")
        self.username = "fallback@chatroom"
        self.table_name = f"Msg_{hashlib.md5(self.username.encode()).hexdigest()}"
        rel_path = "message/message_0.db"
        self.cache_path = os.path.join(
            WeChatDB.CACHE_DIR,
            f"{hashlib.md5(rel_path.encode()).hexdigest()[:12]}.db",
        )
        os.makedirs(WeChatDB.CACHE_DIR, exist_ok=True)
        conn = sqlite3.connect(self.cache_path)
        try:
            conn.execute(
                f"""
                CREATE TABLE [{self.table_name}] (
                    local_type INTEGER,
                    create_time INTEGER,
                    message_content TEXT,
                    WCDB_CT_message_content INTEGER,
                    status INTEGER
                )
                """
            )
            conn.commit()
        finally:
            conn.close()
        self.db = WeChatDB(self.tmp.name, keys={})

    def tearDown(self):
        WeChatDB.CACHE_DIR = self.old_cache_dir
        self.tmp.cleanup()

    def test_refresh_cache_view_preserves_fallback_decrypted_db_files(self):
        before_paths, before_table = self.db._find_msg_table(self.username)

        self.db.refresh_cache_view()
        after_paths, after_table = self.db._find_msg_table(self.username)

        self.assertEqual(before_paths, [self.cache_path])
        self.assertEqual(before_table, self.table_name)
        self.assertEqual(after_paths, [self.cache_path])
        self.assertEqual(after_table, self.table_name)
        self.assertTrue(os.path.exists(self.cache_path))


class WeChatDBPagingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "messages.db")
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                CREATE TABLE Chat_test (
                    local_type INTEGER,
                    create_time INTEGER,
                    message_content TEXT,
                    WCDB_CT_message_content INTEGER,
                    status INTEGER
                )
            """)
            for ts in range(101, 111):
                conn.execute(
                    "INSERT INTO Chat_test VALUES (?, ?, ?, ?, ?)",
                    (1, ts, f"sender:\nmsg{ts}", None, 0),
                )
            conn.commit()
        finally:
            conn.close()

        self.db = object.__new__(WeChatDB)
        self.db._contacts = {"sender": "成员"}
        self.db._nick_to_remark = {}
        self.db._load_contacts = lambda: None
        self.db._find_msg_table = lambda username: ([self.db_path], "Chat_test")

    def tearDown(self):
        self.tmp.cleanup()

    def test_get_messages_default_returns_newest_page_after_bookmark(self):
        messages = self.db.get_messages("room@chatroom", since_ts=100, limit=3)

        self.assertEqual([m["timestamp"] for m in messages], [108, 109, 110])

    def test_get_messages_page_forward_returns_next_page_after_bookmark(self):
        first = self.db.get_messages("room@chatroom", since_ts=100, limit=3, page_forward=True)
        second = self.db.get_messages(
            "room@chatroom",
            since_ts=first[-1]["timestamp"],
            limit=3,
            page_forward=True,
        )

        self.assertEqual([m["timestamp"] for m in first], [101, 102, 103])
        self.assertEqual([m["timestamp"] for m in second], [104, 105, 106])

    def test_get_messages_can_include_cursor_timestamp_for_identity_dedup(self):
        messages = self.db.get_messages(
            "room@chatroom",
            since_ts=103,
            limit=3,
            page_forward=True,
            since_inclusive=True,
        )

        self.assertEqual([m["timestamp"] for m in messages], [103, 104, 105])


class WeChatSourceEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "message_7.db")
        self.username = "room@chatroom"
        self.table_name = "Chat_source"
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(f"""
                CREATE TABLE [{self.table_name}] (
                    local_id INTEGER,
                    server_id INTEGER,
                    sort_seq INTEGER,
                    local_type INTEGER,
                    create_time INTEGER,
                    message_content TEXT,
                    WCDB_CT_message_content INTEGER,
                    status INTEGER,
                    packed_info_data BLOB
                )
            """)
            file_xml = (
                "sender:\n<msg><appmsg><title>reliable source.pdf</title><type>6</type>"
                "<appattach><totallen>12</totallen><fileext>pdf</fileext>"
                "<md5>0123456789abcdef0123456789abcdef</md5><attachid>att-7</attachid>"
                "</appattach></appmsg></msg>"
            )
            conn.execute(
                f"INSERT INTO [{self.table_name}] VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (7, 7007, 70, 49, 100, file_xml, None, 0, None),
            )
            image_hash = "abcdef0123456789abcdef0123456789"
            packed = b"\x0a\x20" + image_hash.encode("ascii")
            conn.execute(
                f"INSERT INTO [{self.table_name}] VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (8, 8008, 80, 3, 101, "sender:\n<msg><img /></msg>", None, 0, packed),
            )
            conn.commit()
        finally:
            conn.close()

        self.db = object.__new__(WeChatDB)
        self.db._contacts = {"sender": "成员"}
        self.db._nick_to_remark = {}
        self.db._load_contacts = lambda: None
        self.db._find_msg_table = lambda username: ([self.db_path], self.table_name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_file_metadata_is_parsed_before_text_cleaning(self):
        message = self.db.get_messages(self.username, limit=10)[0]

        self.assertEqual(message["text"], "[文件] reliable source.pdf")
        self.assertRegex(message["source_message_id"], r"^wgmsg_[0-9a-f]{32}$")
        self.assertEqual(message["source_envelope"]["local_id"], 7)
        self.assertEqual(message["source_envelope"]["server_id"], 7007)
        self.assertEqual(message["source_envelope"]["sort_seq"], 70)
        self.assertTrue(message["source_envelope"]["rowid"])
        self.assertNotIn("room@chatroom", json.dumps(message["source_envelope"]))
        self.assertEqual(message["resources"], [{
            "kind": "file",
            "resource_index": 0,
            "original_name": "reliable source.pdf",
            "extension": "pdf",
            "declared_size": 12,
            "declared_hash": "0123456789abcdef0123456789abcdef",
            "md5": "0123456789abcdef0123456789abcdef",
            "attach_id": "att-7",
            "source_message_id": message["source_message_id"],
        }])

    def test_image_packed_hash_is_preserved_without_raw_blob(self):
        message = self.db.get_messages(self.username, limit=10)[1]

        self.assertEqual(message["text"], "[图片]")
        self.assertTrue(message["source_envelope"]["packed_info_present"])
        self.assertNotIn("packed_info_data", message["source_envelope"])
        self.assertEqual(message["resources"][0]["declared_hash"], "abcdef0123456789abcdef0123456789")

    def test_ai_format_excludes_source_envelope_and_internal_ids(self):
        messages = self.db.get_messages(self.username, limit=10)
        formatted = self.db.format_messages_for_ai(messages, show_group_nickname=True)

        self.assertIn("[文件] reliable source.pdf", formatted)
        self.assertNotIn("wgmsg_", formatted)
        self.assertNotIn("server_id", formatted)
        self.assertNotIn("message_7.db", formatted)


class WeChatImageDecoderTests(unittest.TestCase):
    def test_v2_image_data_decodes_with_saved_key(self):
        key = b"1234567890abcdef"
        image = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" * 10
        aes_size = len(image)
        padding_size = 16 - (aes_size % 16)
        padded = image + bytes([padding_size]) * padding_size
        encrypted = AES.new(key, AES.MODE_ECB).encrypt(padded)
        data = (
            b"\x07\x08\x56\x32\x08\x07"
            + struct.pack("<I", aes_size)
            + struct.pack("<I", 0)
            + b"\x01"
            + encrypted
        )

        decoded = decode_wechat_image_data(data, key.hex())

        self.assertEqual(decoded, image)
        self.assertEqual(detect_mime(decoded), "image/jpeg")

    def test_v2_image_data_without_key_returns_none(self):
        data = b"\x07\x08\x56\x32\x08\x07" + b"\x00" * 32

        self.assertIsNone(decode_wechat_image_data(data, ""))


class WeChatMediaPagingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "media.db")
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                CREATE TABLE Chat_media (
                    local_type INTEGER,
                    create_time INTEGER,
                    message_content TEXT,
                    WCDB_CT_message_content INTEGER,
                    status INTEGER,
                    packed_info_data BLOB
                )
            """)
            for ts in range(101, 111):
                conn.execute(
                    "INSERT INTO Chat_media VALUES (?, ?, ?, ?, ?, ?)",
                    (3, ts, "sender:\n<msg><img /></msg>", None, 0, None),
                )
            for ts in range(201, 211):
                md5 = f"{ts:032x}"[-32:]
                content = (
                    "sender:\n"
                    f'<msg><emoji md5="{md5}" cdnurl="https://example.com/{ts}.png" '
                    'fromusername="sender" /></msg>'
                )
                conn.execute(
                    "INSERT INTO Chat_media VALUES (?, ?, ?, ?, ?, ?)",
                    (47, ts, content, None, 0, None),
                )
            conn.commit()
        finally:
            conn.close()

        self.db = object.__new__(WeChatDB)
        self.db._contacts = {"sender": "成员"}
        self.db._nick_to_remark = {}
        self.db._emoticon_map = {}
        self.db._load_contacts = lambda: None
        self.db._load_emoticon_db = lambda: None
        self.db._find_msg_table = lambda username: ([self.db_path], "Chat_media")
        self.db._find_image_file = lambda *args, **kwargs: None

    def tearDown(self):
        self.tmp.cleanup()

    def test_image_messages_since_returns_latest_page_chronological(self):
        messages = self.db.get_image_messages("room@chatroom", since_ts=100, limit=3)

        self.assertEqual([m["timestamp"] for m in messages], [108, 109, 110])

    def test_emoji_messages_since_returns_latest_page_chronological(self):
        messages = self.db.get_emoji_messages("room@chatroom", since_ts=200, limit=3)

        self.assertEqual([m["timestamp"] for m in messages], [208, 209, 210])


if __name__ == "__main__":
    unittest.main()
