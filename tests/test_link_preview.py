import socket
import unittest
from unittest.mock import patch

from core.link_preview import fetch_link_preview, format_link_previews


class FakeResponse:
    def __init__(self, url, status_code=200, headers=None, body=b"", encoding="utf-8"):
        self.url = url
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body
        self.encoding = encoding
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=16384):
        yield self._body

    def close(self):
        self.closed = True


def public_dns(host, *_args, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


class LinkPreviewSafetyTests(unittest.TestCase):
    def test_blocks_local_and_private_targets_before_request(self):
        urls = [
            "http://localhost/admin",
            "http://127.0.0.1/admin",
            "http://169.254.169.254/latest/meta-data",
            "http://192.168.1.2/router",
            "ftp://example.com/file",
            "https://example.com:8443/admin",
        ]

        with patch("core.link_preview.requests.get") as get:
            for url in urls:
                preview = fetch_link_preview(url)
                self.assertEqual(preview["status"], "blocked", url)
            get.assert_not_called()

    def test_blocks_malformed_port_before_request(self):
        with patch("core.link_preview.requests.get") as get:
            preview = fetch_link_preview("https://example.com:not-a-port/path")

        self.assertEqual(preview["status"], "blocked")
        get.assert_not_called()

    def test_blocks_redirect_to_private_target(self):
        responses = [
            FakeResponse(
                "https://example.com/start",
                status_code=302,
                headers={"location": "http://127.0.0.1/admin"},
            )
        ]

        with patch("core.link_preview.socket.getaddrinfo", side_effect=public_dns), \
             patch("core.link_preview.requests.get", side_effect=responses) as get:
            preview = fetch_link_preview("https://example.com/start")

        self.assertEqual(preview["status"], "blocked")
        self.assertIn("redirect", preview["summary"].lower())
        self.assertTrue(responses[0].closed)
        get.assert_called_once()
        self.assertFalse(get.call_args.kwargs["allow_redirects"])

    def test_blocks_malformed_redirect_and_closes_response(self):
        response = FakeResponse(
            "https://example.com/start",
            status_code=302,
            headers={"location": "https://example.com:not-a-port/path"},
        )

        with patch("core.link_preview.socket.getaddrinfo", side_effect=public_dns), \
             patch("core.link_preview.requests.get", return_value=response):
            preview = fetch_link_preview("https://example.com/start")

        self.assertEqual(preview["status"], "blocked")
        self.assertTrue(response.closed)

    def test_allows_public_html_preview_with_mocked_request(self):
        body = (
            b"<html><head><title>Example Launch</title>"
            b"<meta name='description' content='A public release note.'>"
            b"</head><body><p>This is a public page for testing.</p></body></html>"
        )
        response = FakeResponse(
            "https://example.com/post",
            headers={"content-type": "text/html; charset=utf-8"},
            body=body,
        )

        with patch("core.link_preview.socket.getaddrinfo", side_effect=public_dns), \
             patch("core.link_preview.requests.get", return_value=response):
            preview = fetch_link_preview("https://example.com/post")

        self.assertEqual(preview["status"], "ok")
        self.assertEqual(preview["title"], "Example Launch")
        self.assertIn("A public release note", preview["summary"])
        self.assertTrue(response.closed)

    def test_followed_redirect_closes_intermediate_and_final_responses(self):
        redirect = FakeResponse(
            "https://example.com/start",
            status_code=302,
            headers={"location": "https://example.com/final"},
        )
        final = FakeResponse(
            "https://example.com/final",
            headers={"content-type": "text/plain"},
            body=b"public preview text",
        )

        with patch("core.link_preview.socket.getaddrinfo", side_effect=public_dns), \
             patch("core.link_preview.requests.get", side_effect=[redirect, final]):
            preview = fetch_link_preview("https://example.com/start")

        self.assertEqual(preview["status"], "ok")
        self.assertTrue(redirect.closed)
        self.assertTrue(final.closed)

    def test_redacts_sensitive_query_tokens_in_preview_output(self):
        response = FakeResponse(
            "https://example.com/post?token=secret-token&view=1",
            headers={"content-type": "text/html"},
            body=b"<html><head><title>Token Test</title></head><body>plain public text</body></html>",
        )

        with patch("core.link_preview.socket.getaddrinfo", side_effect=public_dns), \
             patch("core.link_preview.requests.get", return_value=response):
            preview = fetch_link_preview("https://example.com/post?token=secret-token&view=1")

        self.assertIn("token=REDACTED", preview["url"])
        self.assertIn("view=1", preview["url"])
        self.assertNotIn("secret-token", preview["url"])

    def test_format_marks_remote_preview_text_as_untrusted(self):
        text = format_link_previews([
            {
                "url": "https://example.com/post",
                "status": "ok",
                "title": "Example Launch",
                "summary": "Remote page text.",
            }
        ])

        self.assertIn("Untrusted remote page context", text)
        self.assertIn("Remote page text.", text)


if __name__ == "__main__":
    unittest.main()
