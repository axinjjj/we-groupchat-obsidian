import unittest
from unittest.mock import patch

from core.link_preview import (
    LINK_PREVIEW_STATE,
    extract_links,
    fetch_link_preview,
    format_link_previews,
    is_wechat_record_url,
)


class LinkPreviewQuarantineTests(unittest.TestCase):
    def test_every_compatibility_fetch_is_zero_network(self):
        url = "https://example.com/post?token=network-secret&view=1"

        with patch("socket.getaddrinfo") as dns, patch("urllib.request.urlopen") as request:
            preview = fetch_link_preview(url, timeout=1, max_bytes=1)

        dns.assert_not_called()
        request.assert_not_called()
        self.assertEqual(preview["state"], LINK_PREVIEW_STATE)
        self.assertEqual(preview["status"], LINK_PREVIEW_STATE)
        self.assertEqual(preview["network_requests"], 0)
        self.assertNotIn("network-secret", str(preview))
        self.assertIn("view=1", preview["url"])

    def test_wechat_record_url_is_recognized_without_fetching(self):
        url = (
            "https://support.weixin.qq.com/cgi-bin/mmsupport-bin/readtemplate"
            "?t=page/favorite_record__w_unsupport&token=record-secret"
        )

        self.assertTrue(is_wechat_record_url(url))
        preview = fetch_link_preview(url)
        self.assertEqual(preview["state"], LINK_PREVIEW_STATE)
        self.assertEqual(preview["network_requests"], 0)
        self.assertNotIn("record-secret", str(preview))

    def test_extraction_keeps_exact_first_url_values(self):
        first = "https://example.com/A?token=Exact-One"
        second = "https://example.com/a?token=Exact-Two"

        self.assertEqual(extract_links(f"{first} {first}"), [first])
        self.assertEqual(extract_links(f"{first} {second}"), [first, second])

    def test_legacy_formatter_redacts_url_and_remote_text(self):
        text = format_link_previews([{
            "url": "https://example.com/post?access_token=url-secret&view=1",
            "status": "error",
            "title": "See https://example.com/title?sig=title-secret",
            "summary": "Failed at https://example.com/error#jwt=summary-secret",
        }])

        for secret in ("url-secret", "title-secret", "summary-secret"):
            self.assertNotIn(secret, text)
        self.assertIn("network_requests=0", text)
        self.assertIn("view=1", text)


if __name__ == "__main__":
    unittest.main()
