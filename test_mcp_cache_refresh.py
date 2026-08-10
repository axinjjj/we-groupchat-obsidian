import unittest
from unittest.mock import patch

import mcp_server


class FakeVerificationDB:
    def __init__(self):
        self.refreshed = False
        self.invalidated = False

    def resolve_username(self, chat_name):
        return "room@chatroom" if chat_name == "目标群" else None

    def refresh_cache_view(self):
        self.refreshed = True

    def invalidate_cache(self):
        self.invalidated = True
        raise AssertionError("send verification must not delete decrypted cache files")

    def get_messages(self, username, since_ts=0, limit=5):
        self.last_query = (username, since_ts, limit)
        return [{"type": 1, "text": "已发送内容"}]


class McpCacheRefreshTests(unittest.TestCase):
    def test_verify_sent_refreshes_cache_view_without_invalidating_files(self):
        db = FakeVerificationDB()

        with patch("mcp_server._get_db", return_value=db), \
             patch("mcp_server.time.sleep"), \
             patch("mcp_server.time.time", return_value=1000):
            verified = mcp_server._verify_sent("已发送内容", "目标群")

        self.assertTrue(verified)
        self.assertTrue(db.refreshed)
        self.assertFalse(db.invalidated)
        self.assertEqual(db.last_query, ("room@chatroom", 970, 5))


if __name__ == "__main__":
    unittest.main()
