import hashlib
import json
import os
import tempfile
import unittest

import requests

from core.google_drive_client import (
    GoogleDriveClient,
    GoogleDriveRetryableError,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeOAuth:
    def __init__(self):
        self.tokens = ["access-one", "access-two"]
        self.invalidations = 0

    def access_token(self):
        return self.tokens[min(self.invalidations, len(self.tokens) - 1)]

    def invalidate_access_token(self):
        self.invalidations += 1


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return self.responses.pop(0)


class GoogleDriveClientTests(unittest.TestCase):
    def test_401_refreshes_in_memory_token_once(self):
        session = FakeSession([
            FakeResponse(401),
            FakeResponse(200, {"id": "file-1", "trashed": False}),
        ])
        oauth = FakeOAuth()
        client = GoogleDriveClient(oauth, session=session)

        item = client.get_file("file-1")

        self.assertEqual(item["id"], "file-1")
        self.assertEqual(oauth.invalidations, 1)
        self.assertEqual(
            [call[2]["headers"]["Authorization"] for call in session.requests],
            ["Bearer access-one", "Bearer access-two"],
        )

    def test_retry_after_is_exposed_for_persistent_worker_backoff(self):
        client = GoogleDriveClient(
            FakeOAuth(),
            session=FakeSession([FakeResponse(429, headers={"Retry-After": "23"})]),
        )

        with self.assertRaises(GoogleDriveRetryableError) as raised:
            client.list_files("trashed = false")

        self.assertEqual(raised.exception.retry_after, 23)
        self.assertEqual(raised.exception.status_code, 429)

    def test_server_failure_is_retryable(self):
        client = GoogleDriveClient(
            FakeOAuth(),
            session=FakeSession([FakeResponse(503)]),
        )

        with self.assertRaises(GoogleDriveRetryableError) as raised:
            client.get_file("file-1")

        self.assertEqual(raised.exception.status_code, 503)

    def test_network_interruption_is_exposed_as_retryable(self):
        class BrokenSession:
            def request(self, *_args, **_kwargs):
                raise requests.ConnectionError("offline")

        client = GoogleDriveClient(FakeOAuth(), session=BrokenSession())

        with self.assertRaisesRegex(GoogleDriveRetryableError, "drive_network_error"):
            client.get_file("file-1")

    def test_small_upload_uses_multipart_and_returns_verification_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "fixture.bin")
            data = b"fixture upload"
            with open(path, "wb") as handle:
                handle.write(data)
            payload = {
                "id": "object-1",
                "size": str(len(data)),
                "sha256Checksum": hashlib.sha256(data).hexdigest(),
                "md5Checksum": hashlib.md5(data).hexdigest(),
                "appProperties": {"wgo_role": "object"},
            }
            session = FakeSession([FakeResponse(200, payload)])
            client = GoogleDriveClient(FakeOAuth(), session=session)

            result = client.upload_file(
                path,
                "object.bin",
                "parent-1",
                {"wgo_role": "object"},
            )

        self.assertEqual(result, payload)
        method, url, request = session.requests[0]
        self.assertEqual(method, "POST")
        self.assertEqual(request["params"]["uploadType"], "multipart")
        self.assertIn("multipart/related", request["headers"]["Content-Type"])
        self.assertIn(data, request["data"])

    def test_property_search_escapes_values_and_limits_to_app_metadata(self):
        session = FakeSession([FakeResponse(200, {"files": []})])
        client = GoogleDriveClient(FakeOAuth(), session=session)

        client.find_by_properties(
            {"wgo_archive_id": "archive'quoted", "wgo_role": "object"},
            parent_id="parent-1",
        )

        query = session.requests[0][2]["params"]["q"]
        self.assertIn("archive\\'quoted", query)
        self.assertIn("'parent-1' in parents", query)
        self.assertIn("trashed = false", query)


if __name__ == "__main__":
    unittest.main()
