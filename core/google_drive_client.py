"""Small testable Google Drive v3 REST client for app-owned files only."""
from __future__ import annotations

import json
import mimetypes
import os
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
                }, **kwargs)
            raise GoogleDriveAuthRequired("drive_unauthorized")
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
        location = response.headers.get("Location")
        if not location:
            raise GoogleDriveError("resumable_location_missing")
        chunk_size = 8 * 1024 * 1024
        offset = 0
        with open(path, "rb") as handle:
            while offset < size:
                chunk = handle.read(chunk_size)
                end = offset + len(chunk) - 1
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
                )
                offset = end + 1
        return self._json(response)
