import hashlib
import json
import os
import tempfile
import unittest

import requests

from core.google_drive_client import (
    GoogleDriveClient,
    GoogleDriveError,
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
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


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

    def _large_upload(self, responses, size=9 * 1024 * 1024):
        tmp = tempfile.TemporaryDirectory()
        path = os.path.join(tmp.name, "large.bin")
        with open(path, "wb") as handle:
            handle.truncate(size)
        session = FakeSession(responses)
        client = GoogleDriveClient(FakeOAuth(), session=session)
        return tmp, session, client, path, size

    def test_resumable_upload_processes_multi_chunk_308_range(self):
        payload = {"id": "large-object"}
        tmp, session, client, path, size = self._large_upload([
            FakeResponse(200, headers={"Location": "https://upload.invalid/session-1"}),
            FakeResponse(308, headers={"Range": "bytes=0-8388607"}),
            FakeResponse(200, payload),
        ])
        with tmp:
            result = client.upload_file(path, "large.bin", "parent", {})

        self.assertEqual(result, payload)
        puts = [request for request in session.requests if request[0] == "PUT"]
        self.assertEqual(puts[0][2]["headers"]["Content-Range"], f"bytes 0-8388607/{size}")
        self.assertEqual(puts[1][2]["headers"]["Content-Range"], f"bytes 8388608-{size - 1}/{size}")

    def test_resumable_upload_resumes_from_partial_range(self):
        payload = {"id": "partial-object"}
        size = 6 * 1024 * 1024
        tmp, session, client, path, _ = self._large_upload([
            FakeResponse(200, headers={"Location": "https://upload.invalid/session-partial"}),
            FakeResponse(308, headers={"Range": "bytes=0-1048575"}),
            FakeResponse(200, payload),
        ], size=size)
        with tmp:
            result = client.upload_file(path, "partial.bin", "parent", {})

        self.assertEqual(result, payload)
        puts = [request for request in session.requests if request[0] == "PUT"]
        self.assertEqual(
            puts[1][2]["headers"]["Content-Range"],
            f"bytes 1048576-{size - 1}/{size}",
        )

    def test_resumable_upload_probes_and_adopts_completed_after_lost_response(self):
        payload = {"id": "completed-object"}
        size = 6 * 1024 * 1024
        tmp, session, client, path, _ = self._large_upload([
            FakeResponse(200, headers={"Location": "https://upload.invalid/session-lost"}),
            requests.ConnectionError("lost final response"),
            FakeResponse(200, payload),
        ], size=size)
        with tmp:
            result = client.upload_file(path, "lost.bin", "parent", {})

        self.assertEqual(result, payload)
        probe = session.requests[-1]
        self.assertEqual(probe[0], "PUT")
        self.assertEqual(probe[2]["headers"]["Content-Range"], f"bytes */{size}")
        self.assertEqual(probe[2]["data"], b"")

    def test_resumable_upload_recovers_503_from_status_probe_offset(self):
        payload = {"id": "recovered-object"}
        size = 6 * 1024 * 1024
        tmp, session, client, path, _ = self._large_upload([
            FakeResponse(200, headers={"Location": "https://upload.invalid/session-503"}),
            FakeResponse(503),
            FakeResponse(308, headers={"Range": "bytes=0-1048575"}),
            FakeResponse(200, payload),
        ], size=size)
        with tmp:
            result = client.upload_file(path, "retry.bin", "parent", {})

        self.assertEqual(result, payload)
        self.assertEqual(
            session.requests[2][2]["headers"]["Content-Range"],
            f"bytes */{size}",
        )
        self.assertEqual(
            session.requests[3][2]["headers"]["Content-Range"],
            f"bytes 1048576-{size - 1}/{size}",
        )

    def test_resumable_upload_restarts_one_expired_session(self):
        payload = {"id": "restarted-object"}
        size = 6 * 1024 * 1024
        tmp, session, client, path, _ = self._large_upload([
            FakeResponse(200, headers={"Location": "https://upload.invalid/session-old"}),
            requests.ConnectionError("interrupted"),
            FakeResponse(404),
            FakeResponse(200, headers={"Location": "https://upload.invalid/session-new"}),
            FakeResponse(200, payload),
        ], size=size)
        with tmp:
            result = client.upload_file(path, "expired.bin", "parent", {})

        self.assertEqual(result, payload)
        posts = [request for request in session.requests if request[0] == "POST"]
        self.assertEqual(len(posts), 2)
        self.assertEqual(session.requests[-1][1], "https://upload.invalid/session-new")

    def test_resumable_upload_rejects_malformed_or_regressing_range(self):
        size = 6 * 1024 * 1024
        for range_value, code in (("not-a-range", "resumable_range_invalid"), (None, "resumable_range_missing")):
            with self.subTest(range_value=range_value):
                headers = {} if range_value is None else {"Range": range_value}
                tmp, _session, client, path, _ = self._large_upload([
                    FakeResponse(200, headers={"Location": "https://upload.invalid/session-bad"}),
                    FakeResponse(308, headers={"Range": "bytes=0-1048575"}),
                    FakeResponse(308, headers=headers),
                ], size=size)
                with tmp, self.assertRaisesRegex(GoogleDriveError, code):
                    client.upload_file(path, "bad.bin", "parent", {})

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
