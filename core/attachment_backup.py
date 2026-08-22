"""Provider-neutral filesystem snapshots for the local attachment archive."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import tempfile
import time
from datetime import datetime, timezone

from .attachment_archive import ArchiveError, AttachmentArchive
from .config import DATA_DIR
from .knowledge import KNOWLEDGE_DB


BACKUP_SCHEMA = "we-groupchat-obsidian.attachment-backup.v2"
CATALOG_FIELDS = {
    "object_sha256",
    "object_size",
    "original_name",
    "kind",
    "source_message_id",
    "topic_id",
    "event_id",
    "status",
    "resolution_method",
}


def _ensure_private_dir(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _fsync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_json(path, payload, mode=0o600):
    directory = os.path.dirname(path)
    _ensure_private_dir(directory)
    fd, temp_path = tempfile.mkstemp(prefix=".partial-", dir=directory)
    try:
        os.fchmod(fd, mode)
        handle = os.fdopen(fd, "w", encoding="utf-8")
        fd = -1
        with handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = ""
        os.chmod(path, mode)
        _fsync_dir(directory)
    finally:
        if fd >= 0:
            os.close(fd)
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def _hash_fd(fd):
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        digest.update(chunk)
    return size, digest.hexdigest()


def _write_all(fd, data):
    view = memoryview(data)
    written = 0
    while written < len(view):
        count = os.write(fd, view[written:])
        if count <= 0:
            raise OSError("short write")
        written += count


def _hash_path(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        return _hash_fd(fd)
    finally:
        os.close(fd)


def _within(path, root):
    try:
        return os.path.commonpath((os.path.realpath(path), os.path.realpath(root))) == os.path.realpath(root)
    except ValueError:
        return False


class AttachmentBackup:
    """Copy immutable CAS objects to an ordinary filesystem target."""

    def __init__(
        self,
        db_path=KNOWLEDGE_DB,
        archive_root=None,
        target="",
        *,
        now_func=time.time,
        id_factory=None,
    ):
        self.db_path = os.path.abspath(os.path.expanduser(db_path))
        self.archive_root = os.path.abspath(os.path.expanduser(
            archive_root or os.path.join(DATA_DIR, "attachment_archive")
        ))
        self.target = os.path.abspath(os.path.expanduser(target)) if target else ""
        self.now_func = now_func
        self.id_factory = id_factory or (lambda: secrets.token_hex(4))

    @classmethod
    def from_config(cls, config, **kwargs):
        return cls(
            config.get("monitor_knowledge_db") or KNOWLEDGE_DB,
            config.get("attachment_archive_root"),
            config.get("attachment_backup_target") or "",
            **kwargs,
        )

    @property
    def backup_root(self):
        return os.path.join(self.target, "v2") if self.target else ""

    @staticmethod
    def _paths_overlap(left, right):
        try:
            common = os.path.commonpath((left, right))
        except ValueError:
            return False
        return common in {left, right}

    def _target_boundary_error(self):
        if not self.target:
            return ""
        target_forms = {
            os.path.abspath(self.target),
            os.path.realpath(self.target),
        }
        protected_forms = {
            os.path.abspath(self.archive_root),
            os.path.realpath(self.archive_root),
            os.path.abspath(self.db_path),
            os.path.realpath(self.db_path),
        }
        for target in target_forms:
            if os.path.dirname(target) == target:
                return "target_is_filesystem_root"
            for protected in protected_forms:
                if self._paths_overlap(target, protected):
                    return "target_overlaps_local_source"
        return ""

    @staticmethod
    def _invalid_target_result(error_code, **values):
        return {"state": "invalid_target", "error_code": error_code, **values}

    def _snapshot_data(self):
        knowledge_available = False
        knowledge_error = False
        knowledge_objects = []
        knowledge_catalog = []
        if os.path.exists(self.db_path):
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("BEGIN")
                knowledge_objects = [
                    dict(row)
                    for row in conn.execute(
                        """
                        SELECT sha256, size, object_relpath, original_name
                        FROM attachment_objects
                        ORDER BY sha256
                        """
                    )
                ]
                knowledge_catalog = [
                    dict(row)
                    for row in conn.execute(
                        """
                        SELECT
                            COALESCE(o.sha256, '') AS object_sha256,
                            o.size AS object_size,
                            COALESCE(m.original_name, o.original_name, '') AS original_name,
                            m.kind,
                            m.source_message_id,
                            m.topic_id,
                            m.event_id,
                            m.status,
                            m.resolution_method
                        FROM attachment_mentions m
                        LEFT JOIN attachment_objects o ON o.sha256 = m.object_sha256
                        ORDER BY object_sha256, m.mention_id
                        """
                    )
                ]
                conn.commit()
                knowledge_available = True
            except sqlite3.Error:
                conn.rollback()
                knowledge_error = True
            finally:
                conn.close()

        try:
            cas_snapshot = AttachmentArchive(
                self.db_path,
                self.archive_root,
            ).cas_catalog_snapshot()
        except sqlite3.Error:
            cas_snapshot = None
        if knowledge_error or (not knowledge_available and cas_snapshot is None):
            raise sqlite3.OperationalError("attachment catalog is unavailable")

        objects_by_sha = {}

        def merge_object(row):
            digest = str(row.get("sha256") or "")
            size = int(row.get("size") or 0)
            relpath = str(row.get("object_relpath") or "")
            original_name = str(row.get("original_name") or "")
            existing = objects_by_sha.get(digest)
            if existing and int(existing["size"]) != size:
                raise sqlite3.OperationalError("CAS object identity conflict")
            if existing:
                if relpath:
                    existing["object_relpath"] = relpath
                if not existing.get("original_name") and original_name:
                    existing["original_name"] = original_name
                return
            objects_by_sha[digest] = {
                "sha256": digest,
                "size": size,
                "object_relpath": relpath,
                "original_name": original_name,
            }

        for row in knowledge_objects:
            merge_object(row)
        for row in (cas_snapshot or {}).get("objects", []):
            merge_object(row)

        catalog = [
            {
                "object_sha256": str(row.get("object_sha256") or ""),
                "object_size": (
                    int(row["object_size"])
                    if row.get("object_size") is not None
                    else None
                ),
                "original_name": str(row.get("original_name") or ""),
                "kind": str(row.get("kind") or ""),
                "source_message_id": str(row.get("source_message_id") or ""),
                "topic_id": row.get("topic_id"),
                "event_id": row.get("event_id"),
                "status": str(row.get("status") or ""),
                "resolution_method": str(row.get("resolution_method") or ""),
            }
            for row in knowledge_catalog
        ]
        catalog_keys = {
            (
                entry["source_message_id"],
                entry["kind"],
                entry["original_name"],
                entry["object_sha256"],
            )
            for entry in catalog
        }
        for row in (cas_snapshot or {}).get("sources", []):
            digest = str(row.get("object_sha256") or "")
            obj = objects_by_sha.get(digest)
            entry = {
                "object_sha256": digest,
                "object_size": int(obj["size"]) if obj else None,
                "original_name": str(row.get("original_name") or ""),
                "kind": str(row.get("kind") or ""),
                "source_message_id": str(row.get("source_message_id") or ""),
                "topic_id": None,
                "event_id": None,
                "status": str(row.get("status") or ""),
                "resolution_method": str(row.get("resolution_method") or ""),
            }
            key = (
                entry["source_message_id"],
                entry["kind"],
                entry["original_name"],
                entry["object_sha256"],
            )
            if key not in catalog_keys:
                catalog.append(entry)
                catalog_keys.add(key)

        referenced = {
            entry["object_sha256"] for entry in catalog if entry["object_sha256"]
        }
        for digest, row in objects_by_sha.items():
            if digest in referenced:
                continue
            catalog.append({
                "object_sha256": digest,
                "object_size": int(row["size"]),
                "original_name": str(row.get("original_name") or ""),
                "kind": "",
                "source_message_id": "",
                "topic_id": None,
                "event_id": None,
                "status": "orphaned_object",
                "resolution_method": "",
            })

        catalog.sort(key=lambda entry: (
            entry["object_sha256"],
            entry["source_message_id"],
            entry["original_name"],
        ))
        return {
            "objects": [objects_by_sha[key] for key in sorted(objects_by_sha)],
            "catalog": catalog,
        }

    def _target_object_path(self, digest):
        return os.path.join(self.backup_root, "objects", "sha256", digest[:2], digest)

    def _target_object_state(self, row):
        path = self._target_object_path(row["sha256"])
        if not os.path.lexists(path):
            return "missing"
        try:
            mode = os.lstat(path).st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                return "target_failed"
            size, digest = _hash_path(path)
        except OSError:
            return "target_failed"
        if size == int(row["size"]) and digest == row["sha256"]:
            return "target_verified"
        return "target_failed"

    def plan(self):
        states = {
            "target_copied": 0,
            "target_verified": 0,
            "target_failed": 0,
            "missing": 0,
        }
        boundary_error = self._target_boundary_error()
        if boundary_error:
            return self._invalid_target_result(
                boundary_error,
                objects=0,
                bytes=0,
                statuses=states,
            )
        try:
            snapshot = self._snapshot_data()
        except sqlite3.Error:
            return {"state": "catalog_unavailable", "objects": 0, "bytes": 0, "statuses": states}
        rows = snapshot["objects"]
        if not self.target:
            return {
                "state": "target_not_configured",
                "objects": len(rows),
                "bytes": sum(int(row["size"]) for row in rows),
                "statuses": states,
            }
        for row in rows:
            state = self._target_object_state(row)
            states[state] += 1
        return {
            "state": "ready" if states["target_failed"] == 0 else "target_failed",
            "objects": len(rows),
            "bytes": sum(int(row["size"]) for row in rows),
            "statuses": states,
        }

    def _source_path(self, row):
        path = os.path.join(self.archive_root, str(row["object_relpath"] or ""))
        if not _within(path, self.archive_root):
            raise ArchiveError("source_outside_archive")
        try:
            mode = os.lstat(path).st_mode
        except OSError as exc:
            raise ArchiveError("source_missing") from exc
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise ArchiveError("source_not_regular")
        return path

    def _copy_object(self, row):
        target_path = self._target_object_path(row["sha256"])
        existing_state = self._target_object_state(row)
        if existing_state == "target_verified":
            return "target_verified"
        if existing_state == "target_failed":
            raise ArchiveError("target_object_conflict")

        source_path = self._source_path(row)
        directory = os.path.dirname(target_path)
        _ensure_private_dir(directory)
        source_fd = os.open(
            source_path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        target_fd = -1
        temp_path = ""
        try:
            before = os.fstat(source_fd)
            if not stat.S_ISREG(before.st_mode):
                raise ArchiveError("source_not_regular")
            target_fd, temp_path = tempfile.mkstemp(prefix=".partial-", dir=directory)
            os.fchmod(target_fd, 0o600)
            digest = hashlib.sha256()
            size = 0
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                _write_all(target_fd, chunk)
                size += len(chunk)
                digest.update(chunk)
            os.fsync(target_fd)
            after = os.fstat(source_fd)
            if any(
                getattr(before, field) != getattr(after, field)
                for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns")
            ):
                raise ArchiveError("source_changed")
            if size != int(row["size"]) or digest.hexdigest() != row["sha256"]:
                raise ArchiveError("source_hash_mismatch")
            os.close(target_fd)
            target_fd = -1
            os.replace(temp_path, target_path)
            temp_path = ""
            os.chmod(target_path, 0o600)
            _fsync_dir(directory)
            return "target_copied"
        finally:
            os.close(source_fd)
            if target_fd >= 0:
                os.close(target_fd)
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def _snapshot_id(self):
        stamp = datetime.fromtimestamp(self.now_func(), tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"{stamp}-{self.id_factory()}"

    def run(self):
        if not self.target:
            return {
                "state": "target_not_configured",
                "snapshot_id": "",
                "statuses": {"target_copied": 0, "target_verified": 0, "target_failed": 0},
            }
        boundary_error = self._target_boundary_error()
        if boundary_error:
            return self._invalid_target_result(
                boundary_error,
                snapshot_id="",
                statuses={"target_copied": 0, "target_verified": 0, "target_failed": 0},
            )
        try:
            snapshot = self._snapshot_data()
        except sqlite3.Error:
            return {
                "state": "catalog_unavailable",
                "snapshot_id": "",
                "statuses": {"target_copied": 0, "target_verified": 0, "target_failed": 0},
            }
        rows = snapshot["objects"]
        _ensure_private_dir(self.target)
        _ensure_private_dir(self.backup_root)
        statuses = {"target_copied": 0, "target_verified": 0, "target_failed": 0}
        for row in rows:
            try:
                state = self._copy_object(row)
                statuses[state] += 1
            except (ArchiveError, OSError):
                statuses["target_failed"] += 1

        snapshot_id = self._snapshot_id()
        receipt = {
            "schema": BACKUP_SCHEMA,
            "snapshot_id": snapshot_id,
            "state": "complete" if statuses["target_failed"] == 0 else "target_failed",
            "object_count": len(rows),
            "total_bytes": sum(int(row["size"]) for row in rows),
            "statuses": statuses,
        }
        _atomic_json(os.path.join(self.backup_root, "receipts", snapshot_id + ".json"), receipt)
        if statuses["target_failed"]:
            return receipt

        snapshot_dir = os.path.join(self.backup_root, "snapshots", snapshot_id)
        _ensure_private_dir(snapshot_dir)
        manifest = {
            "schema": BACKUP_SCHEMA,
            "snapshot_id": snapshot_id,
            "created_at": datetime.fromtimestamp(self.now_func(), tz=timezone.utc).isoformat(),
            "object_count": len(rows),
            "total_bytes": sum(int(row["size"]) for row in rows),
            "objects": [
                {"sha256": row["sha256"], "size": int(row["size"])}
                for row in rows
            ],
            "catalog_file": "catalog.json",
            "catalog_entry_count": len(snapshot["catalog"]),
        }
        catalog = {
            "schema": BACKUP_SCHEMA,
            "snapshot_id": snapshot_id,
            "entry_count": len(snapshot["catalog"]),
            "entries": snapshot["catalog"],
        }
        _atomic_json(os.path.join(snapshot_dir, "manifest.json"), manifest)
        _atomic_json(os.path.join(snapshot_dir, "catalog.json"), catalog)
        _, manifest_sha256 = _hash_path(os.path.join(snapshot_dir, "manifest.json"))
        _, catalog_sha256 = _hash_path(os.path.join(snapshot_dir, "catalog.json"))
        _atomic_json(
            os.path.join(snapshot_dir, "COMPLETE"),
            {
                "schema": BACKUP_SCHEMA,
                "snapshot_id": snapshot_id,
                "state": "complete",
                "manifest_sha256": manifest_sha256,
                "catalog_sha256": catalog_sha256,
            },
        )
        return receipt

    def _snapshot_dirs(self):
        root = os.path.join(self.backup_root, "snapshots")
        if not root or not os.path.isdir(root):
            return []
        dirs = []
        for entry in os.scandir(root):
            if not entry.is_dir(follow_symlinks=False):
                continue
            if os.path.isfile(os.path.join(entry.path, "COMPLETE")):
                dirs.append(entry.path)
        return sorted(dirs)

    def _load_snapshot(self, snapshot_id=""):
        if not self.target:
            return None
        if snapshot_id:
            directory = os.path.join(self.backup_root, "snapshots", os.path.basename(snapshot_id))
        else:
            snapshots = self._snapshot_dirs()
            directory = snapshots[-1] if snapshots else ""
        if not directory or not os.path.isfile(os.path.join(directory, "COMPLETE")):
            return None
        try:
            with open(os.path.join(directory, "COMPLETE"), encoding="utf-8") as handle:
                complete = json.load(handle)
            with open(os.path.join(directory, "manifest.json"), "rb") as handle:
                manifest_bytes = handle.read()
            with open(os.path.join(directory, "catalog.json"), "rb") as handle:
                catalog_bytes = handle.read()
            manifest = json.loads(manifest_bytes.decode("utf-8"))
            catalog = json.loads(catalog_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not all(isinstance(value, dict) for value in (complete, manifest, catalog)):
            return None
        if (
            complete.get("manifest_sha256") != hashlib.sha256(manifest_bytes).hexdigest()
            or complete.get("catalog_sha256") != hashlib.sha256(catalog_bytes).hexdigest()
        ):
            return None
        snapshot_identity = manifest.get("snapshot_id")
        objects = manifest.get("objects")
        entries = catalog.get("entries")
        if not isinstance(objects, list) or not isinstance(entries, list):
            return None
        try:
            counts_match = (
                int(manifest.get("object_count", -1)) == len(objects)
                and int(manifest.get("total_bytes", -1))
                == sum(int(row["size"]) for row in objects)
                and int(manifest.get("catalog_entry_count", -1)) == len(entries)
                and int(catalog.get("entry_count", -1)) == len(entries)
            )
        except (KeyError, TypeError, ValueError):
            return None
        objects_valid = all(
            isinstance(row, dict)
            and re.fullmatch(r"[0-9a-f]{64}", str(row.get("sha256") or ""))
            and isinstance(row.get("size"), int)
            and row["size"] >= 0
            for row in objects
        )
        object_sizes = {
            row["sha256"]: row["size"]
            for row in objects
            if isinstance(row, dict) and "sha256" in row and "size" in row
        }
        objects_valid = objects_valid and len(object_sizes) == len(objects)
        entries_valid = all(
            isinstance(entry, dict)
            and set(entry) == CATALOG_FIELDS
            and (
                entry["object_sha256"] == ""
                or re.fullmatch(r"[0-9a-f]{64}", str(entry["object_sha256"] or ""))
            )
            and (
                (
                    entry["object_sha256"] == ""
                    and entry["object_size"] is None
                )
                or (
                    entry["object_sha256"] in object_sizes
                    and isinstance(entry["object_size"], int)
                    and entry["object_size"] == object_sizes[entry["object_sha256"]]
                )
            )
            and all(
                isinstance(entry[key], str)
                for key in (
                    "original_name",
                    "kind",
                    "source_message_id",
                    "status",
                    "resolution_method",
                )
            )
            and all(entry[key] is None or isinstance(entry[key], int) for key in ("topic_id", "event_id"))
            for entry in entries
        )
        if (
            complete.get("schema") != BACKUP_SCHEMA
            or complete.get("state") != "complete"
            or complete.get("snapshot_id") != snapshot_identity
            or manifest.get("schema") != BACKUP_SCHEMA
            or manifest.get("catalog_file") != "catalog.json"
            or catalog.get("schema") != BACKUP_SCHEMA
            or catalog.get("snapshot_id") != snapshot_identity
            or not counts_match
            or not objects_valid
            or not entries_valid
        ):
            return None
        return {"manifest": manifest, "catalog": catalog}

    def verify(self, snapshot_id=""):
        snapshot = self._load_snapshot(snapshot_id)
        if snapshot is None:
            return {"state": "snapshot_unavailable", "target_verified": 0, "target_failed": 0}
        manifest = snapshot["manifest"]
        target_verified = 0
        target_failed = 0
        for row in manifest["objects"]:
            if self._target_object_state(row) == "target_verified":
                target_verified += 1
            else:
                target_failed += 1
        return {
            "state": "target_verified" if target_failed == 0 else "target_failed",
            "snapshot_id": manifest["snapshot_id"],
            "target_verified": target_verified,
            "target_failed": target_failed,
        }

    def restore_plan(self, snapshot_id=""):
        snapshot = self._load_snapshot(snapshot_id)
        if snapshot is None:
            return {
                "state": "snapshot_unavailable",
                "snapshot_id": "",
                "restore_objects": 0,
                "restore_bytes": 0,
                "target_failed": 0,
            }
        manifest = snapshot["manifest"]
        restore_objects = 0
        restore_bytes = 0
        target_failed = 0
        for row in manifest["objects"]:
            if self._target_object_state(row) != "target_verified":
                target_failed += 1
                continue
            if self._local_object_verified(row):
                continue
            restore_objects += 1
            restore_bytes += int(row["size"])
        return {
            "state": "ready" if target_failed == 0 else "target_failed",
            "snapshot_id": manifest["snapshot_id"],
            "restore_objects": restore_objects,
            "restore_bytes": restore_bytes,
            "target_failed": target_failed,
            "catalog_entries": len(snapshot["catalog"]["entries"]),
        }

    def _local_object_verified(self, row):
        digest = str(row["sha256"])
        directory = os.path.join(self.archive_root, "objects", "sha256", digest[:2])
        if not os.path.isdir(directory):
            return False
        try:
            entries = os.scandir(directory)
        except OSError:
            return False
        with entries:
            for entry in entries:
                if not entry.name.startswith(digest + "--"):
                    continue
                try:
                    if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                        continue
                    size, actual = _hash_path(entry.path)
                except OSError:
                    continue
                if size == int(row["size"]) and actual == digest:
                    return True
        return False

    def status(self):
        if not self.target:
            return {"state": "target_not_configured", "complete_snapshots": 0}
        boundary_error = self._target_boundary_error()
        if boundary_error:
            return self._invalid_target_result(boundary_error, complete_snapshots=0)
        snapshots = self._snapshot_dirs()
        return {
            "state": "configured",
            "target_exists": os.path.isdir(self.target),
            "complete_snapshots": len(snapshots),
            "latest_snapshot": os.path.basename(snapshots[-1]) if snapshots else "",
        }
