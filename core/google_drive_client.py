"""Small testable Google Drive v3 REST client for app-owned files only."""
from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets

import requests

from .google_drive_auth import GoogleDriveAuthError, GoogleDriveAuthRequired


DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
FILE_FIELDS = (
    "id,name,size,sha256Checksum,md5Checksum,mimeType,appProperties,parents,"
    "trashed,webViewLink,shortcutDetails"
)


class GoogleDriveError(RuntimeError):
    def __init__(self, code: str, *, status_code=0, retry_after=0):
        super().__init__(code)
        self.code = code
        self.status_code = int(status_code or 0)
        self.retry_after = max(0, int(retry_after or 0))


class GoogleDriveRetryableError(GoogleDriveError):
    pass


def _escape_query(value) -> str:
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


class GoogleDriveClient:
    def __init__(self, oauth, *, session=None, timeout=60):
        self.oauth = oauth
        self.session = session or requests.Session()
        self.timeout = timeout

    @staticmethod
    def _retry_after(response):
        try:
            return int(response.headers.get("Retry-After") or 0)
        except (TypeError, ValueError):
            return 0

    def _request(self, method, url, *, retry_auth=True, **kwargs):
        headers = dict(kwargs.pop("headers", {}) or {})
        allowed_statuses = set(kwargs.pop("allowed_statuses", ()) or ())
        try:
            headers["Authorization"] = "Bearer " + self.oauth.access_token()
        except GoogleDriveAuthRequired:
            raise
        except GoogleDriveAuthError as exc:
            raise GoogleDriveRetryableError(exc.code) from exc
        try:
            response = self.session.request(
                method,
                url,
                headers=headers,
                timeout=kwargs.pop("timeout", self.timeout),
                **kwargs,
            )
        except requests.RequestException as exc:
            raise GoogleDriveRetryableError("drive_network_error") from exc
        if response.status_code == 401:
            self.oauth.invalidate_access_token()
            if retry_auth:
                return self._request(method, url, retry_auth=False, headers={
                    key: value for key, value in headers.items() if key.lower() != "authorization"
                }, allowed_statuses=allowed_statuses, **kwargs)
            raise GoogleDriveAuthRequired("drive_unauthorized")
        if response.status_code in allowed_statuses:
            return response
        if response.status_code == 429 or response.status_code >= 500:
            raise GoogleDriveRetryableError(
                f"drive_http_{response.status_code}",
                status_code=response.status_code,
                retry_after=self._retry_after(response),
            )
        if response.status_code >= 400:
            code = f"drive_http_{response.status_code}"
            try:
                payload = response.json()
                reason = payload.get("error", {}).get("errors", [{}])[0].get("reason")
                if reason:
                    code = "drive_" + str(reason)
            except (ValueError, AttributeError, IndexError, TypeError):
                pass
            raise GoogleDriveError(code, status_code=response.status_code)
        return response

    @staticmethod
    def _resumable_offset(response, total, previous_offset, *, allow_empty=False):
        range_value = response.headers.get("Range") or response.headers.get("range")
        if not range_value:
            if allow_empty and int(previous_offset) == 0:
                return 0
            raise GoogleDriveError("resumable_range_missing", status_code=response.status_code)
        match = re.fullmatch(r"bytes=0-([0-9]+)", str(range_value).strip())
        if not match:
            raise GoogleDriveError("resumable_range_invalid", status_code=response.status_code)
        confirmed = int(match.group(1)) + 1
        if confirmed < int(previous_offset):
            raise GoogleDriveError("resumable_offset_regressed", status_code=response.status_code)
        if confirmed >= int(total):
            raise GoogleDriveError(
                "resumable_range_complete_without_result",
                status_code=response.status_code,
            )
        return confirmed

    def _start_resumable_session(self, metadata, mime_type, size):
        response = self._request(
            "POST",
            f"{DRIVE_UPLOAD_API}/files",
            params={"uploadType": "resumable", "fields": FILE_FIELDS},
            headers={
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": mime_type,
                "X-Upload-Content-Length": str(size),
            },
            data=json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )
        location = response.headers.get("Location") or response.headers.get("location")
        if not location:
            raise GoogleDriveError("resumable_location_missing")
        return location

    def _probe_resumable_session(self, location, mime_type, size, previous_offset):
        response = self._request(
            "PUT",
            location,
            headers={
                "Content-Type": mime_type,
                "Content-Length": "0",
                "Content-Range": f"bytes */{size}",
            },
            data=b"",
            timeout=max(self.timeout, 180),
            allowed_statuses=(308, 404),
        )
        if response.status_code == 404:
            return "expired", None
        if response.status_code in {200, 201}:
            return "complete", self._json(response)
        if response.status_code == 308:
            return "resume", self._resumable_offset(
                response,
                size,
                previous_offset,
                allow_empty=True,
            )
        raise GoogleDriveError(
            "resumable_probe_unexpected",
            status_code=response.status_code,
        )

    @staticmethod
    def _json(response):
        try:
            return response.json()
        except ValueError as exc:
            raise GoogleDriveError("drive_response_invalid") from exc

    def get_file(self, file_id: str) -> dict:
        response = self._request(
            "GET",
            f"{DRIVE_API}/files/{file_id}",
            params={"fields": FILE_FIELDS, "supportsAllDrives": "true"},
        )
        return self._json(response)

    def list_files(self, query: str) -> list[dict]:
        files = []
        page_token = ""
        while True:
            params = {
                "q": query,
                "fields": f"nextPageToken,files({FILE_FIELDS})",
                "spaces": "drive",
                "pageSize": 100,
            }
            if page_token:
                params["pageToken"] = page_token
            payload = self._json(self._request("GET", f"{DRIVE_API}/files", params=params))
            files.extend(payload.get("files") or [])
            page_token = str(payload.get("nextPageToken") or "")
            if not page_token:
                return files

    def find_by_properties(self, properties: dict, *, parent_id="", mime_type="") -> list[dict]:
        clauses = ["trashed = false"]
        for key, value in sorted(properties.items()):
            clauses.append(
                "appProperties has { key='%s' and value='%s' }"
                % (_escape_query(key), _escape_query(value))
            )
        if parent_id:
            clauses.append(f"'{_escape_query(parent_id)}' in parents")
        if mime_type:
            clauses.append(f"mimeType = '{_escape_query(mime_type)}'")
        return self.list_files(" and ".join(clauses))

    def list_children(self, parent_id: str) -> list[dict]:
        return self.list_files(
            f"'{_escape_query(parent_id)}' in parents and trashed = false"
        )

    def create_folder(self, name: str, parent_id: str, app_properties: dict) -> dict:
        metadata = {
            "name": name,
            "mimeType": FOLDER_MIME,
            "appProperties": app_properties,
        }
        if parent_id:
            metadata["parents"] = [parent_id]
        return self._json(self._request(
            "POST",
            f"{DRIVE_API}/files",
            params={"fields": FILE_FIELDS},
            json=metadata,
        ))

    def create_shortcut(
        self,
        name: str,
        target_id: str,
        parent_id: str,
        app_properties: dict,
    ) -> dict:
        return self._json(self._request(
            "POST",
            f"{DRIVE_API}/files",
            params={"fields": FILE_FIELDS},
            json={
                "name": name,
                "mimeType": SHORTCUT_MIME,
                "parents": [parent_id],
                "shortcutDetails": {"targetId": target_id},
                "appProperties": app_properties,
            },
        ))

    def upload_file(
        self,
        path: str,
        name: str,
        parent_id: str,
        app_properties: dict,
        *,
        mime_type="",
    ) -> dict:
        size = os.path.getsize(path)
        mime_type = mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
        metadata = {
            "name": name,
            "parents": [parent_id],
            "appProperties": app_properties,
        }
        if size <= 5 * 1024 * 1024:
            boundary = "wgo-" + secrets.token_hex(16)
            with open(path, "rb") as handle:
                content = handle.read()
            body = (
                f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
                + json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
                + f"\r\n--{boundary}\r\nContent-Type: {mime_type}\r\n\r\n"
            ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("ascii")
            return self._json(self._request(
                "POST",
                f"{DRIVE_UPLOAD_API}/files",
                params={"uploadType": "multipart", "fields": FILE_FIELDS},
                headers={"Content-Type": f"multipart/related; boundary={boundary}"},
                data=body,
            ))

        location = self._start_resumable_session(metadata, mime_type, size)
        chunk_size = 8 * 1024 * 1024
        offset = 0
        session_restarts = 0
        stalled_responses = 0
        with open(path, "rb") as handle:
            while offset < size:
                handle.seek(offset)
                chunk = handle.read(min(chunk_size, size - offset))
                end = offset + len(chunk) - 1
                try:
                    response = self._request(
                        "PUT",
                        location,
                        headers={
                            "Content-Type": mime_type,
                            "Content-Length": str(len(chunk)),
                            "Content-Range": f"bytes {offset}-{end}/{size}",
                        },
                        data=chunk,
                        timeout=max(self.timeout, 180),
                        allowed_statuses=(308, 404),
                    )
                except GoogleDriveRetryableError:
                    probe_state, probe_value = self._probe_resumable_session(
                        location, mime_type, size, offset
                    )
                    if probe_state == "complete":
                        return probe_value
                    if probe_state == "expired":
                        if session_restarts >= 1:
                            raise GoogleDriveError("resumable_session_expired")
                        session_restarts += 1
                        location = self._start_resumable_session(metadata, mime_type, size)
                        offset = 0
                        stalled_responses = 0
                        continue
                    confirmed = int(probe_value)
                else:
                    if response.status_code == 404:
                        if session_restarts >= 1:
                            raise GoogleDriveError("resumable_session_expired")
                        session_restarts += 1
                        location = self._start_resumable_session(metadata, mime_type, size)
                        offset = 0
                        stalled_responses = 0
                        continue
                    if response.status_code in {200, 201}:
                        return self._json(response)
                    if response.status_code != 308:
                        raise GoogleDriveError(
                            "resumable_upload_unexpected",
                            status_code=response.status_code,
                        )
                    confirmed = self._resumable_offset(response, size, offset)
                if confirmed == offset:
                    stalled_responses += 1
                    if stalled_responses > 2:
                        raise GoogleDriveError("resumable_no_progress")
                else:
                    stalled_responses = 0
                offset = confirmed
        raise GoogleDriveError("resumable_completed_without_result")
