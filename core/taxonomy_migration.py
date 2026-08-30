"""Sealed, read-only previews for exact taxonomy migrations."""
from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import sys
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from core.knowledge import KnowledgeStore


_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_OPERATION_NAMESPACE_RE = re.compile(r"^[0-9a-f]{32}$")
_CLASSIFICATIONS = ("pending", "applied", "already_clean", "drifted")
_SCHEMA_VERSION = 2
_RENAME_SWAP = 0x00000002
_RENAME_EXCL = 0x00000004
_RENAME_NOFOLLOW_ANY = 0x00000010


class MigrationError(RuntimeError):
    """Privacy-safe taxonomy migration failure with a stable code."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def canonical_json_bytes(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _reject_symlink_components(path: Path, *, include_leaf: bool = True) -> None:
    """Reject caller-controlled symlink components, including existing ancestors.

    Darwin exposes /var, /tmp, and /etc as fixed system aliases.  Resolve that
    one root alias before checking the caller-controlled suffix so normal
    tempfile paths remain usable while nested/configured symlinks are refused.
    """
    absolute = path.expanduser().absolute()
    parts = absolute.parts
    current = Path(parts[0])
    start = 1
    if len(parts) > 1 and str(current / parts[1]) in {"/var", "/tmp", "/etc"}:
        current = Path(os.path.realpath(current / parts[1]))
        start = 2
    limit = len(parts) if include_leaf else max(start, len(parts) - 1)
    for index in range(start, limit):
        current = current / parts[index]
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise MigrationError("invalid_inputs", "configured path is invalid") from exc
        if stat.S_ISLNK(mode):
            raise MigrationError("symlink_refused", "configured path uses a symlink")


def _read_regular_file(path: Path, *, code: str = "source_state_invalid") -> bytes:
    """Read one regular file without following a swapped leaf symlink."""
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode):
            raise MigrationError(code, "expected artifact is not a regular file")
        fd = os.open(path, flags)
    except MigrationError:
        raise
    except (FileNotFoundError, OSError) as exc:
        raise MigrationError(code, "expected artifact is absent or unreadable") from exc
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise MigrationError(code, "artifact changed during validation")
        chunks = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _file_evidence(path: Path, *, code: str = "source_state_invalid") -> dict:
    data = _read_regular_file(path, code=code)
    return {"sha256": sha256_bytes(data), "size": len(data)}


def _generated_relative_parts(inputs: dict, relative_path: str) -> tuple[str, ...]:
    _validated_vault_path(inputs, relative_path)
    relative = Path(relative_path.replace("\\", "/"))
    try:
        parts = relative.relative_to(Path(inputs["obsidian_subdir"])).parts
    except ValueError as exc:
        raise MigrationError(
            "path_outside_generated_subdir", "manifest path is outside generated content"
        ) from exc
    if not parts:
        raise MigrationError("path_outside_generated_subdir", "manifest path is invalid")
    return parts


def _open_generated_parent(
    inputs: dict, relative_path: str, *, create: bool = False
) -> tuple[int, int, str]:
    """Open a managed leaf's parent without following any path component."""
    parts = _generated_relative_parts(inputs, relative_path)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_before = os.stat(inputs["generated_root"], follow_symlinks=False)
        root_fd = os.open(inputs["generated_root"], flags)
    except OSError as exc:
        raise MigrationError("symlink_refused", "generated root is invalid") from exc
    current_fd = root_fd
    try:
        root_opened = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(root_opened.st_mode)
            or (root_before.st_dev, root_before.st_ino)
            != (root_opened.st_dev, root_opened.st_ino)
        ):
            raise MigrationError("symlink_refused", "generated root is invalid")
        for part in parts[:-1]:
            try:
                next_fd = os.open(part, flags, dir_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o755, dir_fd=current_fd)
                next_fd = os.open(part, flags, dir_fd=current_fd)
            if not stat.S_ISDIR(os.fstat(next_fd).st_mode):
                os.close(next_fd)
                raise MigrationError("symlink_refused", "managed directory is invalid")
            if current_fd != root_fd:
                os.close(current_fd)
            current_fd = next_fd
        return root_fd, current_fd, parts[-1]
    except Exception:
        if current_fd != root_fd:
            os.close(current_fd)
        os.close(root_fd)
        raise


def _close_generated_parent(root_fd: int, parent_fd: int) -> None:
    if parent_fd != root_fd:
        os.close(parent_fd)
    os.close(root_fd)


def _read_at(
    parent_fd: int, leaf: str, *, code: str, include_identity: bool = False
) -> tuple[bytes, dict]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        before = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise MigrationError(code, "expected artifact is not a regular file")
        fd = os.open(leaf, flags, dir_fd=parent_fd)
    except MigrationError:
        raise
    except OSError as exc:
        raise MigrationError(code, "expected artifact is absent or unreadable") from exc
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise MigrationError(code, "artifact changed during validation")
        chunks = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        data = b"".join(chunks)
        evidence = {
            "sha256": sha256_bytes(data),
            "size": len(data),
            "mode": stat.S_IMODE(opened.st_mode),
        }
        if include_identity:
            evidence.update({"device": opened.st_dev, "inode": opened.st_ino})
        return data, evidence
    finally:
        os.close(fd)


def _evidence_at(parent_fd: int, leaf: str, *, code: str) -> dict:
    _data, evidence = _read_at(parent_fd, leaf, code=code)
    return evidence


def _read_generated_regular(
    inputs: dict, relative_path: str, *, code: str, include_identity: bool = False
) -> tuple[bytes, dict]:
    try:
        root_fd, parent_fd, leaf = _open_generated_parent(inputs, relative_path)
    except FileNotFoundError as exc:
        raise MigrationError(
            code, "expected artifact is absent or unreadable"
        ) from exc
    try:
        return _read_at(
            parent_fd, leaf, code=code, include_identity=include_identity
        )
    finally:
        _close_generated_parent(root_fd, parent_fd)


def _generated_evidence(inputs: dict, relative_path: str):
    try:
        root_fd, parent_fd, leaf = _open_generated_parent(inputs, relative_path)
    except FileNotFoundError:
        return None
    try:
        try:
            return _evidence_at(parent_fd, leaf, code="source_state_invalid")
        except MigrationError:
            try:
                os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return None
            return "invalid"
    finally:
        _close_generated_parent(root_fd, parent_fd)


def _atomic_leaf_function():
    if sys.platform != "darwin":
        raise MigrationError(
            "atomic_leaf_unsupported",
            "guarded leaf mutation is unavailable on this platform",
        )
    try:
        function = ctypes.CDLL(None, use_errno=True).renameatx_np
    except AttributeError as exc:
        raise MigrationError(
            "atomic_leaf_unsupported",
            "guarded leaf mutation is unavailable on this platform",
        ) from exc
    function.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    function.restype = ctypes.c_int
    return function


def _require_atomic_leaf_support() -> None:
    _atomic_leaf_function()


def _probe_atomic_leaf_capabilities(inputs: dict, operation_namespace: str) -> None:
    """Exercise the exact guarded rename flag sets on the generated-root volume."""
    _require_atomic_leaf_support()
    root_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    root_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    probe_leaf = f".taxonomy-migration-{operation_namespace}-capability-probe"
    root_fd = None
    probe_fd = None
    operation_error = None
    cleanup_error = None
    try:
        root_before = os.stat(inputs["generated_root"], follow_symlinks=False)
        root_fd = os.open(inputs["generated_root"], root_flags)
        root_opened = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(root_opened.st_mode)
            or (root_before.st_dev, root_before.st_ino)
            != (root_opened.st_dev, root_opened.st_ino)
        ):
            raise OSError(errno.ELOOP, "generated root changed")
        os.mkdir(probe_leaf, 0o700, dir_fd=root_fd)
        probe_fd = os.open(probe_leaf, root_flags, dir_fd=root_fd)
        for leaf, data in (("a", b"a"), ("b", b"b")):
            fd = os.open(
                leaf,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=probe_fd,
            )
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
        # Every operation is a validated single leaf beneath this already-open
        # directory FD.  RENAME_NOFOLLOW_ANY is available across the supported
        # macOS range; RENAME_RESOLVE_BENEATH was added only in macOS 26 and is
        # unnecessary for these slash-free, dir-FD-relative names.
        _renameatx_np(
            probe_fd,
            "a",
            probe_fd,
            "c",
            _RENAME_EXCL | _RENAME_NOFOLLOW_ANY,
        )
        _renameatx_np(
            probe_fd,
            "c",
            probe_fd,
            "b",
            _RENAME_SWAP | _RENAME_NOFOLLOW_ANY,
        )
        if _read_at(probe_fd, "b", code="atomic_leaf_capability_unsupported")[0] != b"a":
            raise OSError(errno.EINVAL, "swap verification failed")
        if _read_at(probe_fd, "c", code="atomic_leaf_capability_unsupported")[0] != b"b":
            raise OSError(errno.EINVAL, "swap verification failed")
    except Exception as exc:
        operation_error = exc
    finally:
        if probe_fd is not None:
            for leaf in ("a", "b", "c"):
                try:
                    mode = os.stat(leaf, dir_fd=probe_fd, follow_symlinks=False).st_mode
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    cleanup_error = cleanup_error or exc
                    continue
                if not stat.S_ISREG(mode):
                    cleanup_error = cleanup_error or OSError(
                        errno.EPERM, "probe artifact is not a regular file"
                    )
                    continue
                try:
                    os.unlink(leaf, dir_fd=probe_fd)
                except OSError as exc:
                    cleanup_error = cleanup_error or exc
            try:
                os.fsync(probe_fd)
            except OSError as exc:
                cleanup_error = cleanup_error or exc
            os.close(probe_fd)
        if root_fd is not None:
            try:
                os.rmdir(probe_leaf, dir_fd=root_fd)
                os.fsync(root_fd)
            except FileNotFoundError:
                pass
            except OSError as exc:
                cleanup_error = cleanup_error or exc
                try:
                    os.rmdir(probe_leaf, dir_fd=root_fd)
                    os.fsync(root_fd)
                except FileNotFoundError:
                    pass
                except OSError as retry_exc:
                    cleanup_error = cleanup_error or retry_exc
            os.close(root_fd)
    if cleanup_error is not None:
        raise MigrationError(
            "atomic_leaf_probe_cleanup_failed",
            "atomic leaf capability probe cleanup failed",
        ) from cleanup_error
    if operation_error is not None:
        raise MigrationError(
            "atomic_leaf_capability_unsupported",
            "guarded leaf flags are unsupported on the generated-root volume",
        ) from operation_error


@contextmanager
def _operation_lock(run_dir: str):
    """Hold the private run-level nonblocking mutation lock."""
    run_path = Path(run_dir).expanduser().absolute()
    _reject_symlink_components(run_path)
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(run_path / "operation.lock", flags)
    except OSError as exc:
        raise MigrationError("operation_lock_invalid", "run operation lock is invalid") from exc
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_size != 0
        ):
            raise MigrationError("operation_lock_invalid", "run operation lock is invalid")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MigrationError(
                "operation_lock_busy", "another migration operation is active"
            ) from exc
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _renameatx_np(
    source_parent_fd: int,
    source_leaf: str,
    destination_parent_fd: int,
    destination_leaf: str,
    flags: int,
) -> None:
    """Perform one Darwin dir-FD-relative guarded rename operation."""
    function = _atomic_leaf_function()
    result = function(
        source_parent_fd,
        os.fsencode(source_leaf),
        destination_parent_fd,
        os.fsencode(destination_leaf),
        flags,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _semantic_file_evidence(evidence: dict) -> dict:
    return {
        "sha256": evidence["sha256"],
        "size": evidence["size"],
        "mode": evidence["mode"],
    }


def _fault_point(_name: str, _record_id: str) -> None:
    """Test-only crash boundary; production calls are intentionally inert."""


def _leaf_evidence_or_none(parent_fd: int, leaf: str, *, code: str):
    try:
        return _evidence_at(parent_fd, leaf, code=code)
    except MigrationError:
        try:
            os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        raise


def _atomic_replace_generated(
    inputs: dict,
    relative_path: str,
    data: bytes,
    mode: int,
    *,
    expected: dict | None,
    drift_code: str,
    expected_identity: tuple[int, int] | None = None,
    staging_leaf: str | None = None,
    record_id: str = "unbound",
    before_rename=None,
    cleanup_ready=None,
) -> None:
    root_fd, parent_fd, leaf = _open_generated_parent(
        inputs, relative_path, create=True
    )
    temp_leaf = staging_leaf or f".{leaf}.taxonomy-unbound-staging"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    created = False
    ledgered = False
    try:
        expected_payload = {
            "sha256": sha256_bytes(data), "size": len(data), "mode": mode
        }
        primary = _leaf_evidence_or_none(parent_fd, leaf, code=drift_code)
        staged = _leaf_evidence_or_none(parent_fd, temp_leaf, code=drift_code)
        if expected is None:
            if primary == expected_payload and staged is None:
                return
            if primary is not None or (staged is not None and staged != expected_payload):
                raise MigrationError(drift_code, "managed file state changed")
            if staged is None:
                fd = os.open(temp_leaf, flags, 0o600, dir_fd=parent_fd)
                created = True
                try:
                    os.fchmod(fd, mode)
                    view = memoryview(data)
                    while view:
                        written = os.write(fd, view)
                        view = view[written:]
                    os.fsync(fd)
                finally:
                    os.close(fd)
            if before_rename is not None:
                before_rename()
                ledgered = True
            rename_flags = _RENAME_EXCL | _RENAME_NOFOLLOW_ANY
            try:
                _renameatx_np(parent_fd, temp_leaf, parent_fd, leaf, rename_flags)
            except OSError as exc:
                if exc.errno in {errno.EEXIST, errno.ENOENT, errno.ELOOP}:
                    raise MigrationError(
                        drift_code, "managed file state changed"
                    ) from exc
                raise MigrationError(
                    "atomic_leaf_failed", "guarded leaf create failed"
                ) from exc
            created = False
        else:
            if primary == expected_payload and staged == expected:
                if cleanup_ready is not None:
                    cleanup_ready()
                os.unlink(temp_leaf, dir_fd=parent_fd)
                os.fsync(parent_fd)
                return
            if primary == expected_payload and staged is None:
                return
            if primary != expected or (staged is not None and staged != expected_payload):
                raise MigrationError(drift_code, "managed file state changed")
            _unused, observed = _read_at(
                parent_fd, leaf, code=drift_code, include_identity=True
            )
            if expected_identity is not None:
                if (observed["device"], observed["inode"]) != expected_identity:
                    raise MigrationError(drift_code, "managed file identity changed")
            if staged is None:
                fd = os.open(temp_leaf, flags, 0o600, dir_fd=parent_fd)
                created = True
                try:
                    os.fchmod(fd, mode)
                    view = memoryview(data)
                    while view:
                        written = os.write(fd, view)
                        view = view[written:]
                    os.fsync(fd)
                finally:
                    os.close(fd)
            if before_rename is not None:
                before_rename()
                ledgered = True
            rename_flags = _RENAME_SWAP | _RENAME_NOFOLLOW_ANY
            try:
                _renameatx_np(parent_fd, temp_leaf, parent_fd, leaf, rename_flags)
            except OSError as exc:
                raise MigrationError(
                    drift_code, "managed file state changed"
                ) from exc
            created = False
            _fault_point("after_swap_rename", record_id)
            displaced_matches = False
            try:
                _unused, displaced = _read_at(
                    parent_fd,
                    temp_leaf,
                    code=drift_code,
                    include_identity=True,
                )
                displaced_matches = displaced == observed
            except MigrationError:
                displaced_matches = False
            if not displaced_matches:
                try:
                    _renameatx_np(
                        parent_fd, temp_leaf, parent_fd, leaf, rename_flags
                    )
                except (OSError, MigrationError) as exc:
                    raise MigrationError(
                        "atomic_restore_failed",
                        "guarded leaf swap could not be restored",
                    ) from exc
                raise MigrationError(drift_code, "managed file state changed")
            try:
                installed = _evidence_at(
                    parent_fd, leaf, code="file_write_failed"
                )
            except MigrationError:
                installed = None
            if installed != expected_payload:
                try:
                    _renameatx_np(
                        parent_fd, temp_leaf, parent_fd, leaf, rename_flags
                    )
                except (OSError, MigrationError) as exc:
                    raise MigrationError(
                        "atomic_restore_failed",
                        "guarded leaf swap could not be restored",
                    ) from exc
                # The unexpected bytes now live at the random temp name.  Do
                # not clean them up: they may be a concurrent user write.
                created = False
                raise MigrationError(drift_code, "managed file state changed")
            if cleanup_ready is not None:
                cleanup_ready()
            os.unlink(temp_leaf, dir_fd=parent_fd)
        evidence = _evidence_at(parent_fd, leaf, code="file_write_failed")
        if evidence != {
            "sha256": sha256_bytes(data), "size": len(data), "mode": mode
        }:
            raise MigrationError("file_write_failed", "destination verification failed")
        os.fsync(parent_fd)
    finally:
        if created and not ledgered:
            try:
                os.unlink(temp_leaf, dir_fd=parent_fd)
            except OSError:
                pass
        _close_generated_parent(root_fd, parent_fd)


def _unlink_generated(
    inputs: dict,
    relative_path: str,
    *,
    expected: dict,
    drift_code: str,
    expected_identity: tuple[int, int] | None = None,
    quarantine_leaf: str | None = None,
    record_id: str = "unbound",
    before_rename=None,
    cleanup_ready=None,
    cleanup_may_be_complete: bool = False,
) -> None:
    root_fd, parent_fd, leaf = _open_generated_parent(inputs, relative_path)
    quarantine_leaf = quarantine_leaf or f".{leaf}.taxonomy-unbound-quarantine"
    try:
        primary = _leaf_evidence_or_none(parent_fd, leaf, code=drift_code)
        quarantined = _leaf_evidence_or_none(
            parent_fd, quarantine_leaf, code=drift_code
        )
        if primary is None and quarantined is None and cleanup_may_be_complete:
            return
        if primary is None and quarantined == expected:
            if cleanup_ready is not None:
                cleanup_ready()
            os.unlink(quarantine_leaf, dir_fd=parent_fd)
            os.fsync(parent_fd)
            return
        if primary != expected or quarantined is not None:
            raise MigrationError(drift_code, "managed file state changed")
        _unused, observed = _read_at(
            parent_fd, leaf, code=drift_code, include_identity=True
        )
        if expected_identity is not None:
            if (observed["device"], observed["inode"]) != expected_identity:
                raise MigrationError(drift_code, "managed file identity changed")
        if before_rename is not None:
            before_rename()
        rename_flags = _RENAME_EXCL | _RENAME_NOFOLLOW_ANY
        try:
            _renameatx_np(
                parent_fd, leaf, parent_fd, quarantine_leaf, rename_flags
            )
        except OSError as exc:
            raise MigrationError(drift_code, "managed file state changed") from exc
        _fault_point("after_quarantine_rename", record_id)
        try:
            _unused, quarantined = _read_at(
                parent_fd,
                quarantine_leaf,
                code=drift_code,
                include_identity=True,
            )
        except MigrationError:
            quarantined = {}
        if quarantined != observed:
            try:
                _renameatx_np(
                    parent_fd, quarantine_leaf, parent_fd, leaf, rename_flags
                )
            except (OSError, MigrationError) as exc:
                raise MigrationError(
                    "atomic_restore_failed",
                    "guarded leaf quarantine could not be restored",
                ) from exc
            raise MigrationError(drift_code, "managed file state changed")
        if cleanup_ready is not None:
            cleanup_ready()
        os.unlink(quarantine_leaf, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        # Never clean up an unverified quarantine here: it may contain user bytes.
        _close_generated_parent(root_fd, parent_fd)


def atomic_write_private(path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, temp_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        os.chmod(path, 0o600)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        directory_fd = os.open(path.parent, directory_flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def _normalized_mapping(config: dict, key: str) -> dict:
    value = config.get(key) or {}
    if not isinstance(value, dict) or any(
        not isinstance(item_key, str) or not isinstance(item_value, str)
        for item_key, item_value in value.items()
    ):
        raise MigrationError("config_invalid", "migration configuration is invalid")
    return dict(sorted(value.items()))


def _resolved_inputs(config: dict) -> dict:
    if not isinstance(config, dict):
        raise MigrationError("config_invalid", "migration configuration is invalid")
    try:
        store = KnowledgeStore.from_config(config, read_only=True)
    except (TypeError, ValueError, RuntimeError) as exc:
        raise MigrationError("config_invalid", "migration configuration is invalid") from exc
    configured_db = Path(store.db_path).expanduser().absolute()
    configured_vault = Path(store.obsidian_root).expanduser().absolute()
    configured_generated = configured_vault / store.obsidian_subdir
    for path in (configured_db, configured_vault, configured_generated):
        _reject_symlink_components(path)
    try:
        db_path = configured_db.resolve(strict=True)
        vault_root = configured_vault.resolve(strict=True)
        generated_root = configured_generated.resolve(strict=True)
    except OSError as exc:
        raise MigrationError("invalid_inputs", "configured migration inputs are invalid") from exc
    try:
        db_mode = os.lstat(db_path).st_mode
        vault_mode = os.lstat(vault_root).st_mode
        generated_mode = os.lstat(generated_root).st_mode
    except OSError as exc:
        raise MigrationError("invalid_inputs", "configured migration inputs are invalid") from exc
    if (
        not stat.S_ISREG(db_mode)
        or not stat.S_ISDIR(vault_mode)
        or not stat.S_ISDIR(generated_mode)
    ):
        raise MigrationError("invalid_inputs", "configured migration inputs are invalid")
    subdir = str(store.obsidian_subdir).replace("\\", "/")
    relative = Path(subdir)
    if not subdir or relative.is_absolute() or ".." in relative.parts:
        raise MigrationError("invalid_inputs", "configured migration inputs are invalid")
    return {
        "knowledge_db": str(db_path),
        "obsidian_root": str(vault_root),
        "obsidian_subdir": subdir,
        "generated_root": str(generated_root),
    }


def _migration_config(config: dict, inputs: dict) -> dict:
    return {
        "knowledge_db": inputs["knowledge_db"],
        "obsidian_root": inputs["obsidian_root"],
        "obsidian_subdir": inputs["obsidian_subdir"],
        "taxonomy_assignments": _normalized_mapping(
            config, "monitor_chat_taxonomy_profiles"
        ),
        "vault_aliases": _normalized_mapping(config, "monitor_chat_aliases"),
    }


def _config_sha256(migration_config: dict) -> str:
    return sha256_bytes(canonical_json_bytes(migration_config))


def _validate_run_location(run_path: Path, inputs: dict) -> Path:
    absolute = run_path.expanduser().absolute()
    _reject_symlink_components(absolute, include_leaf=False)
    try:
        parent = absolute.parent.resolve(strict=True)
    except OSError as exc:
        raise MigrationError("run_dir_invalid", "run directory parent is invalid") from exc
    candidate = parent / absolute.name
    vault = Path(inputs["obsidian_root"])
    generated = Path(inputs["generated_root"])
    try:
        inside_vault = os.path.commonpath((str(vault), str(candidate))) == str(vault)
        inside_generated = (
            os.path.commonpath((str(generated), str(candidate))) == str(generated)
        )
    except ValueError as exc:
        raise MigrationError("run_dir_invalid", "run directory is invalid") from exc
    if inside_vault or inside_generated:
        raise MigrationError("run_dir_inside_vault", "run directory must be outside vault content")
    return candidate


def _has_symlink_component(root: Path, relative_parts: tuple[str, ...]) -> bool:
    current = root
    try:
        if stat.S_ISLNK(os.lstat(current).st_mode):
            return True
    except OSError:
        return True
    for part in relative_parts:
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            continue
        except OSError:
            return True
        if stat.S_ISLNK(mode):
            return True
    return False


def _validated_vault_path(inputs: dict, relative_path: str) -> Path:
    if not isinstance(relative_path, str):
        raise MigrationError("path_outside_generated_subdir", "manifest path is invalid")
    text = relative_path.replace("\\", "/")
    relative = Path(text)
    subdir = Path(inputs["obsidian_subdir"])
    if (
        not text
        or text != relative.as_posix()
        or relative.is_absolute()
        or ".." in relative.parts
    ):
        raise MigrationError("path_outside_generated_subdir", "manifest path is outside generated content")
    try:
        inside_parts = relative.relative_to(subdir).parts
    except ValueError as exc:
        raise MigrationError(
            "path_outside_generated_subdir",
            "manifest path is outside generated content",
        ) from exc
    root = Path(inputs["generated_root"])
    if _has_symlink_component(root, inside_parts):
        raise MigrationError("symlink_refused", "manifest path uses a symlink")
    candidate = root.joinpath(*inside_parts)
    if os.path.commonpath((str(root), str(candidate))) != str(root):
        raise MigrationError("path_outside_generated_subdir", "manifest path escaped generated content")
    return candidate


def _open_snapshot(db_path: str) -> sqlite3.Connection:
    """Take one WAL-aware read-only snapshot into an in-memory connection."""
    try:
        source = sqlite3.connect(f"file:{quote(db_path)}?mode=ro", uri=True)
        source.row_factory = sqlite3.Row
        source.execute("PRAGMA query_only = ON")
        base = sqlite3.connect(":memory:")
        base.row_factory = sqlite3.Row
        try:
            source.backup(base)
        except Exception:
            base.close()
            raise
        return base
    except sqlite3.Error as exc:
        raise MigrationError("database_snapshot_failed", "read-only database snapshot failed") from exc
    finally:
        try:
            source.close()
        except UnboundLocalError:
            pass


def _row_dict(conn: sqlite3.Connection, query: str, values: tuple = ()) -> dict | None:
    cursor = conn.execute(query, values)
    row = cursor.fetchone()
    if row is None:
        return None
    columns = [column[0] for column in cursor.description]
    return dict(row) if isinstance(row, sqlite3.Row) else dict(zip(columns, row))


def _taxonomy_value(conn: sqlite3.Connection, topic_id: int) -> dict:
    topic = _row_dict(
        conn,
        """
        SELECT topic_id, category, obsidian_path, taxonomy_profile, taxonomy_version
        FROM topics WHERE topic_id = ?
        """,
        (topic_id,),
    )
    if topic is None:
        raise MigrationError("database_state_invalid", "expected topic row is absent")
    cursor = conn.execute(
        """
        SELECT event_id, category, taxonomy_profile, taxonomy_version
        FROM events WHERE topic_id = ? ORDER BY event_id
        """,
        (topic_id,),
    )
    columns = [column[0] for column in cursor.description]
    events = [
        dict(row) if isinstance(row, sqlite3.Row) else dict(zip(columns, row))
        for row in cursor.fetchall()
    ]
    return {"topic": topic, "events": events}


def _render_topic_value(store: KnowledgeStore, conn: sqlite3.Connection, topic_id: int) -> bytes:
    try:
        return store.render_topic_markdown(conn, topic_id).encode("utf-8")
    except (sqlite3.Error, TypeError, ValueError) as exc:
        raise MigrationError("database_state_invalid", "topic render dependency is invalid") from exc


def _topics_for_indexes(store: KnowledgeStore, conn: sqlite3.Connection) -> list[dict]:
    cursor = conn.execute("SELECT * FROM topics ORDER BY topic_id")
    columns = [column[0] for column in cursor.description]
    return [
        store._topic_dict(row if isinstance(row, sqlite3.Row) else dict(zip(columns, row)))
        for row in cursor.fetchall()
    ]


def _render_managed_index_value(
    store: KnowledgeStore,
    conn: sqlite3.Connection,
    relative_path: str,
) -> bytes:
    try:
        topics = _topics_for_indexes(store, conn)
        for spec in store._date_index_specs(topics):
            candidates = {
                str(spec["rel_path"]).replace("\\", "/"),
                store._date_index_fallback_rel_path(spec["rel_path"]).replace("\\", "/"),
            }
            if relative_path in candidates:
                return store._render_date_index(spec).encode("utf-8")
    except (KeyError, sqlite3.Error, TypeError, ValueError) as exc:
        raise MigrationError("database_state_invalid", "managed index dependency is invalid") from exc
    raise MigrationError("database_state_invalid", "managed index dependency is absent")


def _semantic_digest(value) -> str:
    if isinstance(value, bytes):
        return sha256_bytes(value)
    return sha256_bytes(canonical_json_bytes(value))


def _snapshot_counts(conn: sqlite3.Connection) -> dict:
    try:
        return {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("topics", "events", "relations")
        }
    except sqlite3.Error as exc:
        raise MigrationError("database_state_invalid", "database snapshot schema is invalid") from exc


def _make_file_record(
    *,
    sequence: int,
    kind: str,
    inputs: dict,
    source_relative_path: str,
    destination_relative_path: str,
    payload: bytes,
    run_dir: Path,
    operation_namespace: str,
    topic_id: int | None = None,
) -> tuple[dict, tuple[int, int]]:
    source_relative_path = source_relative_path.replace("\\", "/")
    destination_relative_path = destination_relative_path.replace("\\", "/")
    source_path = _validated_vault_path(inputs, source_relative_path)
    destination_path = _validated_vault_path(inputs, destination_relative_path)
    before, opened_evidence = _read_generated_regular(
        inputs,
        source_relative_path,
        code="source_state_invalid",
        include_identity=True,
    )
    before_mode = opened_evidence["mode"]
    if source_path != destination_path and os.path.lexists(destination_path):
        raise MigrationError("source_state_invalid", "source and destination states are ambiguous")
    record_id = f"file-{sequence:06d}"
    payload_relative_path = f"payload/{record_id}.md"
    atomic_write_private(run_dir / payload_relative_path, payload)
    record = {
        "id": record_id,
        "kind": kind,
        "source_relative_path": source_relative_path,
        "destination_relative_path": destination_relative_path,
        "before_sha256": sha256_bytes(before),
        "before_size": len(before),
        "before_mode": before_mode,
        "source_device": opened_evidence["device"],
        "source_inode": opened_evidence["inode"],
        "payload_sha256": sha256_bytes(payload),
        "payload_size": len(payload),
        "payload_relative_path": payload_relative_path,
        "operation_leaves": {
            "staging": (
                f".taxonomy-migration-{operation_namespace}-{record_id}-staging"
            ),
            "quarantine": (
                f".taxonomy-migration-{operation_namespace}-{record_id}-quarantine"
            ),
        },
    }
    if topic_id is not None:
        record["topic_id"] = int(topic_id)
    return record, (opened_evidence["device"], opened_evidence["inode"])


def _projection_manifest_value(projection: dict) -> dict:
    return {
        "profile": projection["profile"],
        "taxonomy_version": int(projection["taxonomy_version"]),
        "topic_changes": projection["topic_changes"],
        "render_topic_ids": [int(value) for value in projection["render_topic_ids"]],
        "managed_date_index_paths": [
            str(value).replace("\\", "/")
            for value in projection["managed_date_index_paths"]
        ],
    }


def preview_migration(config: dict, profile: str, run_dir: str) -> dict:
    """Create a private preview from one read-only SQLite snapshot."""
    if not isinstance(profile, str) or not profile:
        raise MigrationError("projection_invalid", "taxonomy profile is invalid")
    if not isinstance(run_dir, (str, os.PathLike)):
        raise MigrationError("run_dir_invalid", "run directory is invalid")
    inputs = _resolved_inputs(config)
    migration_config = _migration_config(config, inputs)
    run_path = _validate_run_location(Path(run_dir), inputs)
    try:
        os.mkdir(run_path, 0o700)
    except FileExistsError as exc:
        raise MigrationError("run_dir_exists", "run directory already exists") from exc
    except OSError as exc:
        raise MigrationError("run_dir_invalid", "run directory could not be created") from exc

    try:
        os.chmod(run_path, 0o700)
        atomic_write_private(run_path / "operation.lock", b"")
        os.mkdir(run_path / "payload", 0o700)
        os.chmod(run_path / "payload", 0o700)
        operation_namespace = os.urandom(16).hex()
        store = KnowledgeStore.from_config(config, read_only=True)
        base = _open_snapshot(inputs["knowledge_db"])
        shadow = sqlite3.connect(":memory:")
        shadow.row_factory = sqlite3.Row
        try:
            try:
                projection = store.taxonomy_projection(profile, conn=base)
            except ValueError as exc:
                raise MigrationError("projection_invalid", "taxonomy projection is invalid") from exc
            projection_value = _projection_manifest_value(projection)
            base.backup(shadow)
            try:
                store.apply_taxonomy_projection(shadow, projection)
            except ValueError as exc:
                raise MigrationError("projection_drift", "taxonomy projection no longer matches snapshot") from exc
            shadow.commit()

            database_records = []
            topic_after_values = {}
            managed_index_after_values = {}
            for change in projection_value["topic_changes"]:
                topic_id = int(change["topic_id"])
                database_records.append({
                    "id": f"taxonomy-{topic_id}",
                    "kind": "taxonomy",
                    "topic_id": topic_id,
                    "before_sha256": _semantic_digest(_taxonomy_value(base, topic_id)),
                    "after_sha256": _semantic_digest(_taxonomy_value(shadow, topic_id)),
                })
            for topic_id in projection_value["render_topic_ids"]:
                before_value = _render_topic_value(store, base, int(topic_id))
                after_value = _render_topic_value(store, shadow, int(topic_id))
                topic_after_values[int(topic_id)] = after_value
                database_records.append({
                    "id": f"topic-render-{topic_id}",
                    "kind": "topic_render",
                    "topic_id": int(topic_id),
                    "before_sha256": _semantic_digest(before_value),
                    "before_size": len(before_value),
                    "after_sha256": _semantic_digest(after_value),
                    "after_size": len(after_value),
                })
            for index, relative_path in enumerate(
                projection_value["managed_date_index_paths"], 1
            ):
                before_value = _render_managed_index_value(
                    store, base, relative_path
                )
                after_value = _render_managed_index_value(
                    store, shadow, relative_path
                )
                managed_index_after_values[relative_path] = after_value
                database_records.append({
                    "id": f"managed-index-{index:06d}",
                    "kind": "managed_date_index",
                    "relative_path": relative_path,
                    "before_sha256": _semantic_digest(before_value),
                    "before_size": len(before_value),
                    "after_sha256": _semantic_digest(after_value),
                    "after_size": len(after_value),
                })

            files = []
            destinations = set()
            sources = set()
            source_identities = set()
            for topic_id in projection_value["render_topic_ids"]:
                before_row = _row_dict(
                    base,
                    "SELECT obsidian_path FROM topics WHERE topic_id = ?",
                    (topic_id,),
                )
                after_row = _row_dict(
                    shadow,
                    "SELECT obsidian_path FROM topics WHERE topic_id = ?",
                    (topic_id,),
                )
                if before_row is None or after_row is None:
                    raise MigrationError("database_state_invalid", "render topic row is absent")
                record, source_identity = _make_file_record(
                    sequence=len(files) + 1,
                    kind="topic",
                    inputs=inputs,
                    source_relative_path=before_row["obsidian_path"],
                    destination_relative_path=after_row["obsidian_path"],
                    payload=topic_after_values[topic_id],
                    run_dir=run_path,
                    operation_namespace=operation_namespace,
                    topic_id=topic_id,
                )
                if record["source_relative_path"] in sources:
                    raise MigrationError(
                        "duplicate_source_path", "multiple records share a source"
                    )
                if source_identity in source_identities:
                    raise MigrationError(
                        "duplicate_source_identity",
                        "multiple records share one opened source file",
                    )
                if record["destination_relative_path"] in destinations:
                    raise MigrationError("destination_collision", "multiple records share a destination")
                sources.add(record["source_relative_path"])
                source_identities.add(source_identity)
                destinations.add(record["destination_relative_path"])
                files.append(record)

            for relative_path in projection_value["managed_date_index_paths"]:
                record, source_identity = _make_file_record(
                    sequence=len(files) + 1,
                    kind="managed_date_index",
                    inputs=inputs,
                    source_relative_path=relative_path,
                    destination_relative_path=relative_path,
                    payload=managed_index_after_values[relative_path],
                    run_dir=run_path,
                    operation_namespace=operation_namespace,
                )
                if record["source_relative_path"] in sources:
                    raise MigrationError(
                        "duplicate_source_path", "multiple records share a source"
                    )
                if source_identity in source_identities:
                    raise MigrationError(
                        "duplicate_source_identity",
                        "multiple records share one opened source file",
                    )
                if record["destination_relative_path"] in destinations:
                    raise MigrationError("destination_collision", "multiple records share a destination")
                sources.add(record["source_relative_path"])
                source_identities.add(source_identity)
                destinations.add(record["destination_relative_path"])
                files.append(record)

            manifest = {
                "schema_version": _SCHEMA_VERSION,
                "operation_namespace": operation_namespace,
                "inputs": inputs,
                "migration_config": migration_config,
                "config_sha256": _config_sha256(migration_config),
                "profile": profile,
                "taxonomy_version": int(projection["taxonomy_version"]),
                "projection": projection_value,
                "projection_sha256": _semantic_digest(projection_value),
                "source_database": {
                    "mode": "sqlite_snapshot",
                    "counts": _snapshot_counts(base),
                },
                "database_records": database_records,
                "files": files,
            }
        finally:
            shadow.close()
            base.close()

        _validate_manifest(manifest)
        manifest_bytes = canonical_json_bytes(manifest)
        manifest_sha = sha256_bytes(manifest_bytes)
        atomic_write_private(run_path / "manifest.json", manifest_bytes)
        state = {
            "schema_version": _SCHEMA_VERSION,
            "state": "planned",
            "manifest_sha256": manifest_sha,
            "file_operations": {
                record["id"]: "pending" for record in files
            },
        }
        atomic_write_private(run_path / "state.json", canonical_json_bytes(state))
        return {
            "state": "planned",
            "manifest_sha256": manifest_sha,
            "file_count": len(files),
            "topic_change_count": len(projection["topic_changes"]),
        }
    except Exception as exc:
        try:
            shutil.rmtree(run_path)
        except Exception:
            pass
        if os.path.lexists(run_path):
            raise MigrationError("cleanup_failed", "failed preview artifacts could not be removed") from exc
        raise


def _schema_error(message: str = "sealed manifest schema is invalid") -> MigrationError:
    return MigrationError("manifest_schema_invalid", message)


def _exact_keys(value, keys: set[str]) -> bool:
    return isinstance(value, dict) and set(value) == keys


def _valid_hash(value) -> bool:
    return isinstance(value, str) and _HASH_RE.fullmatch(value) is not None


def _valid_mapping(value) -> bool:
    return isinstance(value, dict) and all(
        isinstance(key, str) and isinstance(item, str)
        for key, item in value.items()
    )


def _valid_relative_path(value: object, *, beneath: str | None = None) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        return False
    if beneath is not None:
        try:
            path.relative_to(Path(beneath))
        except ValueError:
            return False
    return True


def _validate_change(change: object, *, obsidian_subdir: str) -> None:
    if not _exact_keys(change, {"topic_id", "before", "after"}) or not _is_int(change["topic_id"]):
        raise _schema_error()
    fields = {"category", "obsidian_path", "taxonomy_profile", "taxonomy_version"}
    for side in (change["before"], change["after"]):
        if not _exact_keys(side, fields):
            raise _schema_error()
        if not all(isinstance(side[key], str) for key in fields - {"taxonomy_version"}):
            raise _schema_error()
        if not _is_int(side["taxonomy_version"]):
            raise _schema_error()
        if not _valid_relative_path(
            side["obsidian_path"], beneath=obsidian_subdir
        ):
            raise _schema_error()


def _validate_manifest(manifest: object) -> None:
    if isinstance(manifest, dict) and manifest.get("schema_version") != _SCHEMA_VERSION:
        raise MigrationError(
            "manifest_schema_version_unsupported",
            "sealed manifest schema version is unsupported",
        )
    top_keys = {
        "schema_version", "operation_namespace", "inputs", "migration_config", "config_sha256",
        "profile", "taxonomy_version", "projection", "projection_sha256", "source_database",
        "database_records", "files",
    }
    if not _exact_keys(manifest, top_keys):
        raise _schema_error()
    operation_namespace = manifest["operation_namespace"]
    if (
        not isinstance(operation_namespace, str)
        or _OPERATION_NAMESPACE_RE.fullmatch(operation_namespace) is None
    ):
        raise _schema_error()
    if not isinstance(manifest["profile"], str) or not manifest["profile"]:
        raise _schema_error()
    if not _is_int(manifest["taxonomy_version"]):
        raise _schema_error()

    input_keys = {"knowledge_db", "obsidian_root", "obsidian_subdir", "generated_root"}
    inputs = manifest["inputs"]
    if not _exact_keys(inputs, input_keys) or not all(
        isinstance(inputs[key], str) and inputs[key] for key in input_keys
    ):
        raise _schema_error()
    if not Path(inputs["knowledge_db"]).is_absolute() or not Path(inputs["obsidian_root"]).is_absolute():
        raise _schema_error()
    if not Path(inputs["generated_root"]).is_absolute() or not _valid_relative_path(inputs["obsidian_subdir"]):
        raise _schema_error()

    config_keys = {
        "knowledge_db", "obsidian_root", "obsidian_subdir",
        "taxonomy_assignments", "vault_aliases",
    }
    migration_config = manifest["migration_config"]
    if not _exact_keys(migration_config, config_keys):
        raise _schema_error()
    if any(
        migration_config[key] != inputs[key]
        for key in ("knowledge_db", "obsidian_root", "obsidian_subdir")
    ):
        raise _schema_error()
    if not _valid_mapping(migration_config["taxonomy_assignments"]) or not _valid_mapping(migration_config["vault_aliases"]):
        raise _schema_error()
    if not _valid_hash(manifest["config_sha256"]) or manifest["config_sha256"] != _config_sha256(migration_config):
        raise _schema_error()

    projection_keys = {
        "profile", "taxonomy_version", "topic_changes",
        "render_topic_ids", "managed_date_index_paths",
    }
    projection = manifest["projection"]
    if not _exact_keys(projection, projection_keys):
        raise _schema_error()
    if projection["profile"] != manifest["profile"] or projection["taxonomy_version"] != manifest["taxonomy_version"]:
        raise _schema_error()
    if (
        not _valid_hash(manifest["projection_sha256"])
        or manifest["projection_sha256"] != _semantic_digest(projection)
    ):
        raise _schema_error()
    if not isinstance(projection["topic_changes"], list):
        raise _schema_error()
    for change in projection["topic_changes"]:
        _validate_change(
            change, obsidian_subdir=inputs["obsidian_subdir"]
        )
    change_ids = [change["topic_id"] for change in projection["topic_changes"]]
    changes_by_id = {
        change["topic_id"]: change for change in projection["topic_changes"]
    }
    render_ids = projection["render_topic_ids"]
    index_paths = projection["managed_date_index_paths"]
    if (
        len(change_ids) != len(set(change_ids))
        or not isinstance(render_ids, list)
        or any(not _is_int(value) for value in render_ids)
        or len(render_ids) != len(set(render_ids))
        or not set(change_ids).issubset(set(render_ids))
        or not isinstance(index_paths, list)
        or any(not _valid_relative_path(value, beneath=inputs["obsidian_subdir"]) for value in index_paths)
        or len(index_paths) != len(set(index_paths))
    ):
        raise _schema_error()

    source_database = manifest["source_database"]
    if not _exact_keys(source_database, {"mode", "counts"}) or source_database["mode"] != "sqlite_snapshot":
        raise _schema_error()
    if not _exact_keys(source_database["counts"], {"topics", "events", "relations"}) or any(
        not _is_int(value) or value < 0 for value in source_database["counts"].values()
    ):
        raise _schema_error()

    records = manifest["database_records"]
    if not isinstance(records, list):
        raise _schema_error()
    record_ids = set()
    taxonomy_ids = set()
    topic_render_ids = set()
    topic_render_records = {}
    managed_paths = set()
    managed_records = {}
    managed_record_ids = {
        path: f"managed-index-{index:06d}"
        for index, path in enumerate(index_paths, 1)
    }
    for record in records:
        if not isinstance(record, dict):
            raise _schema_error()
        kind = record.get("kind")
        common = {"id", "kind", "before_sha256", "after_sha256"}
        if kind in {"topic_render", "managed_date_index"}:
            common |= {"before_size", "after_size"}
        expected = common | ({"relative_path"} if kind == "managed_date_index" else {"topic_id"})
        if kind not in {"taxonomy", "topic_render", "managed_date_index"} or not _exact_keys(record, expected):
            raise _schema_error()
        if not isinstance(record["id"], str) or not record["id"] or record["id"] in record_ids:
            raise _schema_error()
        record_ids.add(record["id"])
        if not _valid_hash(record["before_sha256"]) or not _valid_hash(record["after_sha256"]):
            raise _schema_error()
        if kind in {"topic_render", "managed_date_index"} and (
            not _is_int(record["before_size"])
            or record["before_size"] < 0
            or not _is_int(record["after_size"])
            or record["after_size"] < 0
        ):
            raise _schema_error()
        if kind == "managed_date_index":
            if not _valid_relative_path(record["relative_path"], beneath=inputs["obsidian_subdir"]):
                raise _schema_error()
            if record["id"] != managed_record_ids.get(record["relative_path"]):
                raise _schema_error()
            if record["relative_path"] in managed_records:
                raise _schema_error()
            managed_paths.add(record["relative_path"])
            managed_records[record["relative_path"]] = record
        elif not _is_int(record["topic_id"]):
            raise _schema_error()
        elif kind == "taxonomy":
            if record["id"] != f"taxonomy-{record['topic_id']}":
                raise _schema_error()
            taxonomy_ids.add(record["topic_id"])
        else:
            if record["id"] != f"topic-render-{record['topic_id']}":
                raise _schema_error()
            if record["topic_id"] in topic_render_records:
                raise _schema_error()
            topic_render_ids.add(record["topic_id"])
            topic_render_records[record["topic_id"]] = record
    if taxonomy_ids != set(change_ids) or topic_render_ids != set(render_ids) or managed_paths != set(index_paths):
        raise _schema_error()
    if len(records) != len(change_ids) + len(render_ids) + len(index_paths):
        raise _schema_error()

    files = manifest["files"]
    if not isinstance(files, list):
        raise _schema_error()
    file_ids = set()
    sources = set()
    source_identities = set()
    destinations = set()
    payload_paths = set()
    file_topic_ids = set()
    file_index_paths = set()
    topic_files = {}
    index_files = {}
    common_file = {
        "id", "kind", "source_relative_path", "destination_relative_path",
        "before_sha256", "before_size", "before_mode", "source_device",
        "source_inode", "payload_sha256", "payload_size",
        "payload_relative_path", "operation_leaves",
    }
    for sequence, record in enumerate(files, 1):
        if not isinstance(record, dict):
            raise _schema_error()
        kind = record.get("kind")
        expected = common_file | ({"topic_id"} if kind == "topic" else set())
        if kind not in {"topic", "managed_date_index"} or not _exact_keys(record, expected):
            raise _schema_error()
        if not isinstance(record["id"], str) or not record["id"] or record["id"] in file_ids:
            raise _schema_error()
        if record["id"] != f"file-{sequence:06d}":
            raise _schema_error()
        file_ids.add(record["id"])
        for key in ("source_relative_path", "destination_relative_path"):
            if not _valid_relative_path(record[key], beneath=inputs["obsidian_subdir"]):
                raise _schema_error()
        if record["source_relative_path"] in sources:
            raise _schema_error()
        sources.add(record["source_relative_path"])
        if (
            not _is_int(record["source_device"])
            or record["source_device"] < 0
            or not _is_int(record["source_inode"])
            or record["source_inode"] <= 0
        ):
            raise _schema_error()
        source_identity = (record["source_device"], record["source_inode"])
        if source_identity in source_identities:
            raise _schema_error()
        source_identities.add(source_identity)
        if record["destination_relative_path"] in destinations:
            raise _schema_error()
        destinations.add(record["destination_relative_path"])
        if not _valid_hash(record["before_sha256"]) or not _valid_hash(record["payload_sha256"]):
            raise _schema_error()
        if (
            not _is_int(record["before_size"])
            or record["before_size"] < 0
            or not _is_int(record["before_mode"])
            or record["before_mode"] < 0
            or record["before_mode"] > 0o777
            or not _is_int(record["payload_size"])
            or record["payload_size"] < 0
        ):
            raise _schema_error()
        expected_payload = f"payload/{record['id']}.md"
        if record["payload_relative_path"] != expected_payload or expected_payload in payload_paths:
            raise _schema_error()
        payload_paths.add(expected_payload)
        expected_operation_leaves = {
            "staging": (
                f".taxonomy-migration-{operation_namespace}-{record['id']}-staging"
            ),
            "quarantine": (
                f".taxonomy-migration-{operation_namespace}-{record['id']}-quarantine"
            ),
        }
        if record["operation_leaves"] != expected_operation_leaves:
            raise _schema_error()
        if kind == "topic":
            if not _is_int(record["topic_id"]):
                raise _schema_error()
            if record["topic_id"] in topic_files:
                raise _schema_error()
            file_topic_ids.add(record["topic_id"])
            topic_files[record["topic_id"]] = record
        else:
            if record["source_relative_path"] != record["destination_relative_path"]:
                raise _schema_error()
            if record["source_relative_path"] in index_files:
                raise _schema_error()
            file_index_paths.add(record["source_relative_path"])
            index_files[record["source_relative_path"]] = record
    if file_topic_ids != set(render_ids) or file_index_paths != set(index_paths):
        raise _schema_error()
    if len(files) != len(render_ids) + len(index_paths):
        raise _schema_error()

    for topic_id, file_record in topic_files.items():
        change = changes_by_id.get(topic_id)
        if change is None:
            if (
                file_record["source_relative_path"]
                != file_record["destination_relative_path"]
            ):
                raise _schema_error()
        elif (
            file_record["source_relative_path"]
            != change["before"]["obsidian_path"]
            or file_record["destination_relative_path"]
            != change["after"]["obsidian_path"]
        ):
            raise _schema_error()
        semantic_record = topic_render_records.get(topic_id)
        if semantic_record is None or (
            file_record["payload_sha256"] != semantic_record["after_sha256"]
            or file_record["payload_size"] != semantic_record["after_size"]
        ):
            raise _schema_error()

    for relative_path, file_record in index_files.items():
        semantic_record = managed_records.get(relative_path)
        if semantic_record is None or (
            file_record["payload_sha256"] != semantic_record["after_sha256"]
            or file_record["payload_size"] != semantic_record["after_size"]
        ):
            raise _schema_error()


def _load_canonical_json(path: Path, *, canonical_code: str) -> tuple[dict, bytes]:
    raw = _read_regular_file(path, code="sealed_run_invalid")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationError("sealed_run_invalid", "sealed run artifact is invalid") from exc
    if not isinstance(value, dict):
        raise MigrationError("sealed_run_invalid", "sealed run artifact is invalid")
    try:
        canonical = canonical_json_bytes(value)
    except (TypeError, ValueError) as exc:
        raise MigrationError("sealed_run_invalid", "sealed run artifact is invalid") from exc
    if raw != canonical:
        raise MigrationError(canonical_code, "sealed run artifact is not canonical")
    return value, raw


def _validate_state(state: object) -> None:
    if isinstance(state, dict) and state.get("schema_version") != _SCHEMA_VERSION:
        raise MigrationError(
            "state_schema_version_unsupported",
            "sealed state schema version is unsupported",
        )
    planned_keys = {
        "schema_version", "state", "manifest_sha256", "file_operations"
    }
    ledger_keys = planned_keys | {
        "backups_verified", "database_applied", "applied_file_ids",
        "database_backup_sha256", "database_backup_logical_sha256",
        "database_after_logical_sha256", "file_backup_sha256",
    }
    if not isinstance(state, dict):
        raise MigrationError("state_schema_invalid", "sealed state schema is invalid")
    file_operations = state.get("file_operations")
    operation_phases = {
        "pending", "apply_destination_prepared", "apply_destination_cleanup",
        "apply_source_quarantine_prepared", "apply_source_quarantine_cleanup",
        "applied", "rollback_source_prepared", "rollback_source_cleanup",
        "rollback_destination_quarantine_prepared",
        "rollback_destination_quarantine_cleanup", "rolled_back",
    }
    operations_valid = (
        isinstance(file_operations, dict)
        and all(
            isinstance(key, str) and value in operation_phases
            for key, value in file_operations.items()
        )
    )
    if state.get("state") == "planned":
        valid = _exact_keys(state, planned_keys) and operations_valid
    else:
        valid = (
            state.get("state") in {
                "backups_verified", "applying", "applied", "rolling_back",
                "rolled_back",
            }
            and _exact_keys(state, ledger_keys)
            and operations_valid
            and state.get("backups_verified") is True
            and isinstance(state.get("database_applied"), bool)
            and isinstance(state.get("applied_file_ids"), list)
            and all(isinstance(value, str) for value in state["applied_file_ids"])
            and len(state["applied_file_ids"]) == len(set(state["applied_file_ids"]))
            and _valid_hash(state.get("database_backup_sha256"))
            and _valid_hash(state.get("database_backup_logical_sha256"))
            and _valid_hash(state.get("database_after_logical_sha256"))
            and isinstance(state.get("file_backup_sha256"), dict)
            and all(
                isinstance(key, str) and _valid_hash(value)
                for key, value in state["file_backup_sha256"].items()
            )
        )
    if not valid or not _valid_hash(state.get("manifest_sha256")):
        raise MigrationError("state_schema_invalid", "sealed state schema is invalid")


def _validate_state_manifest_relationship(state: dict, manifest: dict) -> None:
    file_ids = [record["id"] for record in manifest["files"]]
    if set(state["file_operations"]) != set(file_ids):
        raise MigrationError("state_schema_invalid", "sealed state relationships are invalid")
    if state["state"] == "planned":
        if any(value != "pending" for value in state["file_operations"].values()):
            raise MigrationError("state_schema_invalid", "sealed state relationships are invalid")
        return
    applied_ids = state["applied_file_ids"]
    valid = (
        set(state["file_backup_sha256"]) == set(file_ids)
        and set(applied_ids).issubset(set(file_ids))
    )
    if state["state"] == "backups_verified":
        valid = (
            valid and not state["database_applied"] and not applied_ids
            and all(value == "pending" for value in state["file_operations"].values())
        )
    elif state["state"] == "applying":
        valid = valid and state["database_applied"]
    elif state["state"] == "applied":
        valid = (
            valid and state["database_applied"] and applied_ids == file_ids
            and all(value == "applied" for value in state["file_operations"].values())
        )
    elif state["state"] == "rolling_back":
        valid = valid and all(
            value in {
                "applied", "rollback_source_prepared", "rollback_source_cleanup",
                "rollback_destination_quarantine_prepared",
                "rollback_destination_quarantine_cleanup", "rolled_back",
            }
            for value in state["file_operations"].values()
        )
    elif state["state"] == "rolled_back":
        valid = (
            valid and not state["database_applied"] and not applied_ids
            and all(value == "rolled_back" for value in state["file_operations"].values())
        )
    if not valid:
        raise MigrationError("state_schema_invalid", "sealed state relationships are invalid")


def _require_private_mode(
    path: Path, *, expected_mode: int, directory: bool
) -> None:
    try:
        mode = os.lstat(path).st_mode
    except OSError as exc:
        raise MigrationError(
            "sealed_run_invalid", "sealed run artifact is invalid"
        ) from exc
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(mode):
        raise MigrationError(
            "sealed_run_invalid", "sealed run artifact is invalid"
        )
    if stat.S_IMODE(mode) != expected_mode:
        raise MigrationError(
            "privacy_mode_invalid", "sealed run artifact mode is not private"
        )


def _payload_inventory(run_path: Path, manifest: dict) -> set[str]:
    payload_root = run_path / "payload"
    try:
        _require_private_mode(
            payload_root, expected_mode=0o700, directory=True
        )
        actual = set()
        with os.scandir(payload_root) as entries:
            for entry in entries:
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    raise MigrationError("payload_inventory_invalid", "sealed payload inventory is invalid")
                actual.add(f"payload/{entry.name}")
    except FileNotFoundError as exc:
        raise MigrationError("payload_inventory_invalid", "sealed payload inventory is invalid") from exc
    expected = {record["payload_relative_path"] for record in manifest["files"]}
    if actual != expected:
        raise MigrationError("payload_inventory_invalid", "sealed payload inventory is invalid")
    for relative_path in expected:
        _require_private_mode(
            run_path / relative_path, expected_mode=0o600, directory=False
        )
    return expected


def load_sealed_run(run_dir: str) -> tuple[dict, str, dict]:
    """Load and structurally authenticate the canonical Task 2 run."""
    run_path = Path(run_dir).expanduser().absolute()
    _reject_symlink_components(run_path)
    try:
        mode = os.lstat(run_path).st_mode
    except OSError as exc:
        raise MigrationError("sealed_run_invalid", "sealed run directory is invalid") from exc
    if not stat.S_ISDIR(mode):
        raise MigrationError("sealed_run_invalid", "sealed run directory is invalid")
    if stat.S_IMODE(mode) != 0o700:
        raise MigrationError(
            "privacy_mode_invalid", "sealed run directory mode is not private"
        )
    _require_private_mode(
        run_path / "manifest.json", expected_mode=0o600, directory=False
    )
    _require_private_mode(
        run_path / "state.json", expected_mode=0o600, directory=False
    )
    _require_private_mode(
        run_path / "operation.lock", expected_mode=0o600, directory=False
    )
    if os.lstat(run_path / "operation.lock").st_size != 0:
        raise MigrationError("operation_lock_invalid", "run operation lock is invalid")
    manifest, raw = _load_canonical_json(
        run_path / "manifest.json", canonical_code="manifest_not_canonical"
    )
    state, _ = _load_canonical_json(
        run_path / "state.json", canonical_code="state_not_canonical"
    )
    _validate_manifest(manifest)
    _validate_state(state)
    _validate_state_manifest_relationship(state, manifest)
    manifest_sha = sha256_bytes(raw)
    if state["manifest_sha256"] != manifest_sha:
        raise MigrationError("manifest_hash_mismatch", "sealed manifest hash does not match state")
    _payload_inventory(run_path, manifest)
    for record in manifest["files"]:
        payload_path = run_path / record["payload_relative_path"]
        evidence = _file_evidence(payload_path, code="payload_hash_mismatch")
        if evidence != {
            "sha256": record["payload_sha256"],
            "size": record["payload_size"],
        }:
            raise MigrationError("payload_hash_mismatch", "sealed payload hash does not match manifest")
    return manifest, manifest_sha, state


def _sealed_source_identity_matches(inputs: dict, record: dict) -> bool:
    try:
        _unused, evidence = _read_generated_regular(
            inputs,
            record["source_relative_path"],
            code="source_state_invalid",
            include_identity=True,
        )
    except (FileNotFoundError, MigrationError):
        return False
    return (
        evidence["device"] == record["source_device"]
        and evidence["inode"] == record["source_inode"]
    )


def _operation_leaf_evidence(
    inputs: dict, relative_path: str, operation_leaf: str
):
    try:
        root_fd, parent_fd, _leaf = _open_generated_parent(inputs, relative_path)
    except FileNotFoundError:
        return None
    try:
        return _leaf_evidence_or_none(
            parent_fd, operation_leaf, code="file_drift"
        )
    except MigrationError:
        return "invalid"
    finally:
        _close_generated_parent(root_fd, parent_fd)


def _operation_artifacts_match(inputs: dict, record: dict, phase: str) -> bool:
    source_relative = record["source_relative_path"]
    destination_relative = record["destination_relative_path"]
    same_path = source_relative == destination_relative
    before = {
        "sha256": record["before_sha256"], "size": record["before_size"],
        "mode": record["before_mode"],
    }
    payload = {
        "sha256": record["payload_sha256"], "size": record["payload_size"],
        "mode": record["before_mode"],
    }
    source = _generated_evidence(inputs, source_relative)
    destination = source if same_path else _generated_evidence(
        inputs, destination_relative
    )
    staging_leaf = record["operation_leaves"]["staging"]
    quarantine_leaf = record["operation_leaves"]["quarantine"]
    source_staging = _operation_leaf_evidence(
        inputs, source_relative, staging_leaf
    )
    source_quarantine = _operation_leaf_evidence(
        inputs, source_relative, quarantine_leaf
    )
    destination_staging = source_staging if same_path else _operation_leaf_evidence(
        inputs, destination_relative, staging_leaf
    )
    destination_quarantine = (
        source_quarantine if same_path else _operation_leaf_evidence(
            inputs, destination_relative, quarantine_leaf
        )
    )
    if phase == "pending":
        if source_quarantine is not None or destination_quarantine is not None:
            return False
        if same_path:
            return source_staging is None or (
                source == before
                and before != payload
                and source_staging == payload
            )
        return source_staging is None and (
            destination_staging is None
            or (
                source == before
                and destination is None
                and destination_staging == payload
            )
        )
    if phase in {"applied", "rolled_back"}:
        return all(
            value is None
            for value in {
                "source_staging": source_staging,
                "source_quarantine": source_quarantine,
                "destination_staging": destination_staging,
                "destination_quarantine": destination_quarantine,
            }.values()
        )
    if phase.startswith("apply_destination"):
        if source_quarantine is not None or destination_quarantine is not None:
            return False
        if same_path:
            return (source, source_staging) in (
                (before, payload), (payload, before), (payload, None)
            )
        return source_staging is None and source == before and (
            destination, destination_staging
        ) in (
            (None, payload), (payload, None)
        )
    if phase.startswith("apply_source_quarantine"):
        return (
            not same_path
            and source_staging is None
            and destination_staging is None
            and destination_quarantine is None
            and destination == payload
            and (source, source_quarantine) in (
                (before, None), (None, before), (None, None)
            )
        )
    if phase.startswith("rollback_source"):
        if source_quarantine is not None or destination_quarantine is not None:
            return False
        if same_path:
            return (source, source_staging) in (
                (payload, before), (before, payload), (before, None)
            )
        return destination_staging is None and destination == payload and (
            source, source_staging
        ) in (
            (None, before), (before, None)
        )
    if phase.startswith("rollback_destination_quarantine"):
        return (
            not same_path
            and source_staging is None
            and destination_staging is None
            and source_quarantine is None
            and source == before
            and (destination, destination_quarantine) in (
                (payload, None), (None, payload), (None, None)
            )
        )
    return False


def _current_file_classification(
    inputs: dict,
    record: dict,
    *,
    require_source_identity: bool = True,
    operation_phase: str = "pending",
) -> str:
    if not _operation_artifacts_match(inputs, record, operation_phase):
        return "drifted"
    source_relative = record["source_relative_path"]
    destination_relative = record["destination_relative_path"]
    source_evidence = _generated_evidence(inputs, source_relative)
    destination_evidence = (
        source_evidence
        if source_relative == destination_relative
        else _generated_evidence(inputs, destination_relative)
    )
    before = {
        "sha256": record["before_sha256"],
        "size": record["before_size"],
        "mode": record["before_mode"],
    }
    payload = {
        "sha256": record["payload_sha256"],
        "size": record["payload_size"],
        "mode": record["before_mode"],
    }
    if source_relative == destination_relative:
        if source_evidence == before == payload:
            return "already_clean"
        if source_evidence == before:
            return (
                "pending"
                if not require_source_identity
                or _sealed_source_identity_matches(inputs, record)
                else "drifted"
            )
        if source_evidence == payload:
            return "applied"
        return "drifted"
    if source_evidence == before and destination_evidence is None:
        return (
            "pending"
            if not require_source_identity
            or _sealed_source_identity_matches(inputs, record)
            else "drifted"
        )
    if source_evidence is None and destination_evidence == payload:
        return "applied"
    return "drifted"


def _database_digest_for_record(
    store: KnowledgeStore,
    conn: sqlite3.Connection,
    record: dict,
) -> str:
    if record["kind"] == "taxonomy":
        return _semantic_digest(_taxonomy_value(conn, record["topic_id"]))
    if record["kind"] == "topic_render":
        return _semantic_digest(_render_topic_value(store, conn, record["topic_id"]))
    return _semantic_digest(
        _render_managed_index_value(store, conn, record["relative_path"])
    )


def _classification(digest: str, record: dict) -> str:
    before = record["before_sha256"]
    after = record["after_sha256"]
    if digest == before == after:
        return "already_clean"
    if digest == before:
        return "pending"
    if digest == after:
        return "applied"
    return "drifted"


def _overall_state(file_counts: dict, database_counts: dict) -> str:
    if file_counts["drifted"] or database_counts["drifted"]:
        return "drifted"
    has_pending = bool(file_counts["pending"] or database_counts["pending"])
    has_applied = bool(file_counts["applied"] or database_counts["applied"])
    if has_pending and has_applied:
        return "mixed"
    if has_pending:
        return "planned"
    if has_applied:
        return "applied"
    return "already_clean"


def status_migration(config: dict, run_dir: str) -> dict:
    """Return privacy-safe file and SQLite semantic counts from a fresh snapshot."""
    manifest, manifest_sha, ledger = load_sealed_run(run_dir)
    inputs = _resolved_inputs(config)
    migration_config = _migration_config(config, inputs)
    if (
        inputs != manifest["inputs"]
        or migration_config != manifest["migration_config"]
        or _config_sha256(migration_config) != manifest["config_sha256"]
    ):
        raise MigrationError("config_drift", "migration-relevant configuration changed")

    file_counts = {name: 0 for name in _CLASSIFICATIONS}
    for record in manifest["files"]:
        file_counts[_current_file_classification(
            inputs,
            record,
            require_source_identity=ledger["state"] not in {
                "rolling_back", "rolled_back"
            },
            operation_phase=ledger["file_operations"][record["id"]],
        )] += 1

    database_counts = {name: 0 for name in _CLASSIFICATIONS}
    store = KnowledgeStore.from_config(config, read_only=True)
    snapshot = _open_snapshot(inputs["knowledge_db"])
    try:
        for record in manifest["database_records"]:
            try:
                digest = _database_digest_for_record(store, snapshot, record)
                classification = _classification(digest, record)
            except (MigrationError, sqlite3.Error, TypeError, ValueError):
                classification = "drifted"
            database_counts[classification] += 1
    finally:
        snapshot.close()

    state = _overall_state(file_counts, database_counts)
    if ledger["state"] == "rolled_back" and state in {"planned", "already_clean"}:
        state = "rolled_back"
    return {
        "state": state,
        "manifest_sha256": manifest_sha,
        "total": sum(file_counts.values()),
        **file_counts,
        "database_total": sum(database_counts.values()),
        **{f"database_{key}": value for key, value in database_counts.items()},
    }


def _require_manifest_authorization(
    manifest_sha: str, supplied_sha: str, confirm: str, action: str
) -> None:
    if not _valid_hash(supplied_sha) or supplied_sha != manifest_sha:
        raise MigrationError("manifest_hash_mismatch", "full manifest hash does not match")
    expected = f"{action}_TAXONOMY_MIGRATION:{manifest_sha}"
    if confirm != expected:
        raise MigrationError("confirmation_mismatch", "exact confirmation token does not match")


def _write_state(run_path: Path, state: dict) -> None:
    _validate_state(state)
    atomic_write_private(run_path / "state.json", canonical_json_bytes(state))


def _config_from_manifest(manifest: dict) -> dict:
    migration_config = manifest["migration_config"]
    return {
        "monitor_knowledge_db": migration_config["knowledge_db"],
        "monitor_obsidian_root": migration_config["obsidian_root"],
        "monitor_obsidian_subdir": migration_config["obsidian_subdir"],
        "monitor_chat_taxonomy_profiles": migration_config["taxonomy_assignments"],
        "monitor_chat_aliases": migration_config["vault_aliases"],
    }


def _validate_config_against_manifest(config: dict, manifest: dict) -> dict:
    inputs = _resolved_inputs(config)
    migration_config = _migration_config(config, inputs)
    if (
        inputs != manifest["inputs"]
        or migration_config != manifest["migration_config"]
        or _config_sha256(migration_config) != manifest["config_sha256"]
    ):
        raise MigrationError("config_drift", "migration-relevant configuration changed")
    return inputs


def _database_classifications(
    store: KnowledgeStore, conn: sqlite3.Connection, manifest: dict
) -> list[str]:
    values = []
    for record in manifest["database_records"]:
        try:
            digest = _database_digest_for_record(store, conn, record)
            values.append(_classification(digest, record))
        except (MigrationError, sqlite3.Error, TypeError, ValueError) as exc:
            raise MigrationError("database_drift", "database state changed") from exc
    return values


def _database_phase(classifications: list[str], *, applied_hint: bool = False) -> str:
    if any(value == "drifted" for value in classifications):
        raise MigrationError("database_drift", "database state changed")
    before = all(value in {"pending", "already_clean"} for value in classifications)
    after = all(value in {"applied", "already_clean"} for value in classifications)
    if before and after:
        return "after" if applied_hint else "before"
    if before:
        return "before"
    if after:
        return "after"
    raise MigrationError("database_drift", "database state is partially applied")


def _logical_database_sha256(conn: sqlite3.Connection) -> str:
    try:
        return sha256_bytes("\n".join(conn.iterdump()).encode("utf-8"))
    except sqlite3.Error as exc:
        raise MigrationError("database_drift", "database snapshot could not be hashed") from exc


def _verify_database_before(
    store: KnowledgeStore, conn: sqlite3.Connection, manifest: dict
) -> None:
    if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise MigrationError("integrity_check", "SQLite integrity check failed")
    if _snapshot_counts(conn) != manifest["source_database"]["counts"]:
        raise MigrationError("database_drift", "database counts changed")
    if _database_phase(_database_classifications(store, conn, manifest)) != "before":
        raise MigrationError("database_drift", "database backup is not the before state")


def _mkdir_private(path: Path, *, root: Path) -> None:
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        try:
            os.mkdir(current, 0o700)
        except FileExistsError:
            mode = os.lstat(current).st_mode
            if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
                raise MigrationError("backup_invalid", "backup path is invalid")
        os.chmod(current, 0o700)


def _create_sqlite_backup(
    source_path: Path, backup_path: Path, store: KnowledgeStore, manifest: dict
) -> str:
    fd, temp_name = tempfile.mkstemp(prefix=".knowledge.db.", dir=backup_path.parent)
    os.close(fd)
    os.chmod(temp_name, 0o600)
    source = sqlite3.connect(str(source_path))
    target = sqlite3.connect(temp_name)
    try:
        source.backup(target)
        target.commit()
        _verify_database_before(store, target, manifest)
    except Exception:
        target.close()
        source.close()
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    target.close()
    source.close()
    os.replace(temp_name, backup_path)
    os.chmod(backup_path, 0o600)
    return sha256_bytes(_read_regular_file(backup_path, code="backup_invalid"))


def _verify_backup_root(
    backup_root: Path, manifest: dict
) -> tuple[str, str, str, dict[str, str]]:
    _require_private_mode(backup_root, expected_mode=0o700, directory=True)
    database_backup = backup_root / "knowledge.db"
    _require_private_mode(database_backup, expected_mode=0o600, directory=False)
    db_sha = sha256_bytes(_read_regular_file(database_backup, code="backup_invalid"))
    store = KnowledgeStore.from_config(_config_from_manifest(manifest), read_only=True)
    backup_conn = sqlite3.connect(str(database_backup))
    backup_conn.row_factory = sqlite3.Row
    try:
        _verify_database_before(store, backup_conn, manifest)
        db_logical_sha = _logical_database_sha256(backup_conn)
        projected = sqlite3.connect(":memory:")
        projected.row_factory = sqlite3.Row
        try:
            backup_conn.backup(projected)
            store.apply_taxonomy_projection(projected, manifest["projection"])
            projected.commit()
            db_after_logical_sha = _logical_database_sha256(projected)
        finally:
            projected.close()
    except (sqlite3.Error, MigrationError) as exc:
        raise MigrationError("backup_invalid", "database backup verification failed") from exc
    finally:
        backup_conn.close()

    file_hashes = {}
    files_root = backup_root / "files"
    _require_private_mode(files_root, expected_mode=0o700, directory=True)
    expected_files = {"knowledge.db", "inventory.json"}
    expected_dirs = {"files"}
    for record in manifest["files"]:
        relative = Path(record["source_relative_path"])
        backup_path = files_root / relative
        _reject_symlink_components(backup_path)
        _require_private_mode(backup_path, expected_mode=0o600, directory=False)
        evidence = _file_evidence(backup_path, code="backup_invalid")
        if evidence != {
            "sha256": record["before_sha256"],
            "size": record["before_size"],
        }:
            raise MigrationError("backup_invalid", "file backup verification failed")
        file_hashes[record["id"]] = evidence["sha256"]
        expected_files.add((Path("files") / relative).as_posix())
        parent = Path("files")
        for part in relative.parts[:-1]:
            parent /= part
            expected_dirs.add(parent.as_posix())

    actual_files = set()
    actual_dirs = set()
    for dirpath, dirnames, filenames in os.walk(backup_root, followlinks=False):
        base = Path(dirpath)
        for name in list(dirnames):
            child = base / name
            mode = os.lstat(child).st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise MigrationError("backup_invalid", "backup inventory is invalid")
            _require_private_mode(child, expected_mode=0o700, directory=True)
            actual_dirs.add(child.relative_to(backup_root).as_posix())
        for name in filenames:
            child = base / name
            mode = os.lstat(child).st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise MigrationError("backup_invalid", "backup inventory is invalid")
            _require_private_mode(child, expected_mode=0o600, directory=False)
            actual_files.add(child.relative_to(backup_root).as_posix())
    if actual_files != expected_files or actual_dirs != expected_dirs:
        raise MigrationError("backup_invalid", "backup inventory is invalid")

    expected_inventory = {
        "schema_version": _SCHEMA_VERSION,
        "database_backup_sha256": db_sha,
        "database_backup_logical_sha256": db_logical_sha,
        "database_after_logical_sha256": db_after_logical_sha,
        "file_backup_sha256": file_hashes,
    }
    inventory, _raw = _load_canonical_json(
        backup_root / "inventory.json", canonical_code="backup_invalid"
    )
    if inventory != expected_inventory:
        raise MigrationError("backup_invalid", "backup inventory evidence changed")
    return db_sha, db_logical_sha, db_after_logical_sha, file_hashes


def _verify_backup_inventory(
    run_path: Path, manifest: dict, state: dict | None = None
) -> tuple[str, str, str, dict[str, str]]:
    try:
        evidence = _verify_backup_root(run_path / "backups", manifest)
    except MigrationError as exc:
        if exc.code == "backup_invalid":
            raise
        raise MigrationError("backup_invalid", "backup inventory is invalid") from exc
    db_sha, db_logical_sha, db_after_logical_sha, file_hashes = evidence
    if state is not None and (
        state["database_backup_sha256"] != db_sha
        or state["database_backup_logical_sha256"] != db_logical_sha
        or state["database_after_logical_sha256"] != db_after_logical_sha
        or state["file_backup_sha256"] != file_hashes
    ):
        raise MigrationError("backup_invalid", "backup evidence changed")
    return evidence


def _backup_ledger(
    manifest_sha: str,
    evidence: tuple[str, str, str, dict],
    file_operations: dict[str, str],
) -> dict:
    db_sha, db_logical_sha, db_after_logical_sha, file_hashes = evidence
    return {
        "schema_version": _SCHEMA_VERSION,
        "state": "backups_verified",
        "manifest_sha256": manifest_sha,
        "backups_verified": True,
        "database_applied": False,
        "applied_file_ids": [],
        "database_backup_sha256": db_sha,
        "database_backup_logical_sha256": db_logical_sha,
        "database_after_logical_sha256": db_after_logical_sha,
        "file_backup_sha256": file_hashes,
        "file_operations": dict(file_operations),
    }


def _discard_backup_staging(staging: Path) -> None:
    try:
        mode = os.lstat(staging).st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise MigrationError("backup_invalid", "backup staging path is invalid")
    shutil.rmtree(staging)


def _ensure_verified_backups(
    run_path: Path, manifest: dict, state: dict
) -> dict:
    if state["state"] != "planned":
        _verify_backup_inventory(run_path, manifest, state)
        return state
    backup_root = run_path / "backups"
    staging_root = run_path / ".backups.staging"
    if os.path.lexists(backup_root):
        evidence = _verify_backup_inventory(run_path, manifest)
        ledger = _backup_ledger(
            state["manifest_sha256"], evidence, state["file_operations"]
        )
        _write_state(run_path, ledger)
        return ledger
    if os.path.lexists(staging_root):
        try:
            evidence = _verify_backup_root(staging_root, manifest)
        except MigrationError:
            _discard_backup_staging(staging_root)
        else:
            os.replace(staging_root, backup_root)
            ledger = _backup_ledger(
                state["manifest_sha256"], evidence, state["file_operations"]
            )
            _write_state(run_path, ledger)
            return ledger
    os.mkdir(staging_root, 0o700)
    os.chmod(staging_root, 0o700)
    files_root = staging_root / "files"
    os.mkdir(files_root, 0o700)
    os.chmod(files_root, 0o700)
    store = KnowledgeStore.from_config(_config_from_manifest(manifest), read_only=True)
    _create_sqlite_backup(
        Path(manifest["inputs"]["knowledge_db"]),
        staging_root / "knowledge.db",
        store,
        manifest,
    )
    for record in manifest["files"]:
        backup_bytes, evidence = _read_generated_regular(
            manifest["inputs"],
            record["source_relative_path"],
            code="source_state_invalid",
        )
        if evidence != {
            "sha256": record["before_sha256"],
            "size": record["before_size"],
            "mode": record["before_mode"],
        }:
            raise MigrationError("source_state_invalid", "source changed before backup")
        destination = files_root / Path(record["source_relative_path"])
        _mkdir_private(destination.parent, root=files_root)
        atomic_write_private(destination, backup_bytes)
    database_backup = staging_root / "knowledge.db"
    db_sha = sha256_bytes(_read_regular_file(database_backup, code="backup_invalid"))
    backup_conn = sqlite3.connect(str(database_backup))
    backup_conn.row_factory = sqlite3.Row
    try:
        db_logical_sha = _logical_database_sha256(backup_conn)
        projected = sqlite3.connect(":memory:")
        projected.row_factory = sqlite3.Row
        try:
            backup_conn.backup(projected)
            store.apply_taxonomy_projection(projected, manifest["projection"])
            projected.commit()
            db_after_logical_sha = _logical_database_sha256(projected)
        finally:
            projected.close()
    finally:
        backup_conn.close()
    file_hashes = {
        record["id"]: record["before_sha256"] for record in manifest["files"]
    }
    inventory = {
        "schema_version": _SCHEMA_VERSION,
        "database_backup_sha256": db_sha,
        "database_backup_logical_sha256": db_logical_sha,
        "database_after_logical_sha256": db_after_logical_sha,
        "file_backup_sha256": file_hashes,
    }
    atomic_write_private(
        staging_root / "inventory.json", canonical_json_bytes(inventory)
    )
    evidence = _verify_backup_root(staging_root, manifest)
    os.replace(staging_root, backup_root)
    ledger = _backup_ledger(
        state["manifest_sha256"], evidence, state["file_operations"]
    )
    _write_state(run_path, ledger)
    return ledger


def _set_file_operation(
    run_path: Path, state: dict, record_id: str, phase: str
) -> None:
    state["file_operations"][record_id] = phase
    _write_state(run_path, state)


def _apply_file_record(
    run_path: Path, inputs: dict, record: dict, state: dict
) -> None:
    source_relative = record["source_relative_path"]
    destination_relative = record["destination_relative_path"]
    before = {
        "sha256": record["before_sha256"], "size": record["before_size"],
        "mode": record["before_mode"],
    }
    payload = {
        "sha256": record["payload_sha256"], "size": record["payload_size"],
        "mode": record["before_mode"],
    }
    phase = state["file_operations"][record["id"]]
    if phase == "applied":
        return
    source_evidence = _generated_evidence(inputs, source_relative)
    destination_evidence = source_evidence if (
        source_relative == destination_relative
    ) else _generated_evidence(inputs, destination_relative)
    if source_relative == destination_relative:
        if source_evidence == payload and phase == "pending":
            return
        if phase == "pending" and source_evidence != before:
            raise MigrationError("file_drift", "managed file changed")
    else:
        if source_evidence is None and destination_evidence == payload and phase == "pending":
            return
        if phase == "pending" and (
            source_evidence != before or destination_evidence is not None
        ):
            raise MigrationError("file_drift", "managed file state changed")
    payload_bytes = _read_regular_file(
        run_path / record["payload_relative_path"], code="payload_hash_mismatch"
    )
    if phase not in {
        "apply_source_quarantine_prepared",
        "apply_source_quarantine_cleanup",
    }:
        _atomic_replace_generated(
            inputs,
            destination_relative,
            payload_bytes,
            record["before_mode"],
            expected=(before if source_relative == destination_relative else None),
            drift_code="file_drift",
            expected_identity=(
                (record["source_device"], record["source_inode"])
                if source_relative == destination_relative
                else None
            ),
            staging_leaf=record["operation_leaves"]["staging"],
            record_id=record["id"],
            before_rename=lambda: _set_file_operation(
                run_path, state, record["id"], "apply_destination_prepared"
            ),
            cleanup_ready=lambda: _set_file_operation(
                run_path, state, record["id"], "apply_destination_cleanup"
            ),
        )
    if _generated_evidence(inputs, destination_relative) != payload:
        raise MigrationError("file_write_failed", "destination verification failed")
    if source_relative != destination_relative:
        current_phase = state["file_operations"][record["id"]]
        if (
            current_phase not in {
                "apply_source_quarantine_prepared",
                "apply_source_quarantine_cleanup",
            }
            and _generated_evidence(inputs, source_relative) != before
        ):
            raise MigrationError("file_drift", "source changed during apply")
        _unlink_generated(
            inputs,
            source_relative,
            expected=before,
            drift_code="file_drift",
            expected_identity=(record["source_device"], record["source_inode"]),
            quarantine_leaf=record["operation_leaves"]["quarantine"],
            record_id=record["id"],
            before_rename=lambda: _set_file_operation(
                run_path, state, record["id"],
                "apply_source_quarantine_prepared",
            ),
            cleanup_ready=lambda: _set_file_operation(
                run_path, state, record["id"],
                "apply_source_quarantine_cleanup",
            ),
            cleanup_may_be_complete=(
                current_phase == "apply_source_quarantine_cleanup"
            ),
        )


def _is_interrupted_file_record(inputs: dict, record: dict) -> bool:
    if record["source_relative_path"] == record["destination_relative_path"]:
        return False
    return (
        _generated_evidence(inputs, record["source_relative_path"])
        == {
            "sha256": record["before_sha256"], "size": record["before_size"],
            "mode": record["before_mode"],
        }
        and _generated_evidence(inputs, record["destination_relative_path"])
        == {
            "sha256": record["payload_sha256"], "size": record["payload_size"],
            "mode": record["before_mode"],
        }
    )


def _remove_empty_generated_parents(inputs: dict, relative_path: str) -> None:
    parts = _generated_relative_parts(inputs, relative_path)
    subdir = Path(inputs["obsidian_subdir"])
    for depth in range(len(parts) - 1, 0, -1):
        directory_relative = (subdir.joinpath(*parts[:depth])).as_posix()
        try:
            root_fd, parent_fd, leaf = _open_generated_parent(
                inputs, directory_relative
            )
        except (FileNotFoundError, MigrationError):
            return
        try:
            os.rmdir(leaf, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except OSError:
            return
        finally:
            _close_generated_parent(root_fd, parent_fd)


def _apply_database(
    config: dict, manifest: dict, state: dict
) -> tuple[bool, str]:
    inputs = manifest["inputs"]
    store = KnowledgeStore.from_config(config, read_only=False)
    conn = sqlite3.connect(inputs["knowledge_db"])
    conn.row_factory = sqlite3.Row
    try:
        classifications = _database_classifications(store, conn, manifest)
        phase = _database_phase(
            classifications, applied_hint=state.get("database_applied", False)
        )
        if phase == "after":
            return False, "after"
        conn.execute("BEGIN IMMEDIATE")
        fresh_snapshot = _open_snapshot(inputs["knowledge_db"])
        try:
            if (
                _logical_database_sha256(fresh_snapshot)
                != state["database_backup_logical_sha256"]
            ):
                raise MigrationError(
                    "database_drift", "complete database changed after backup"
                )
            fresh_projection = _projection_manifest_value(
                store.taxonomy_projection(manifest["profile"], conn=fresh_snapshot)
            )
        finally:
            fresh_snapshot.close()
        if _semantic_digest(fresh_projection) != manifest["projection_sha256"]:
            raise MigrationError("database_drift", "taxonomy projection changed")
        try:
            store.apply_taxonomy_projection(conn, manifest["projection"])
        except ValueError as exc:
            raise MigrationError("database_drift", "taxonomy projection changed") from exc
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise MigrationError("integrity_check", "SQLite integrity check failed")
        conn.commit()
        return True, "after"
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def apply_migration(
    config: dict, run_dir: str, manifest_sha256: str, confirm: str
) -> dict:
    with _operation_lock(run_dir):
        return _apply_migration_locked(
            config, run_dir, manifest_sha256, confirm
        )


def _apply_migration_locked(
    config: dict, run_dir: str, manifest_sha256: str, confirm: str
) -> dict:
    """Apply one authenticated sealed migration, safely resumable."""
    manifest, manifest_sha, state = load_sealed_run(run_dir)
    _require_manifest_authorization(
        manifest_sha, manifest_sha256, confirm, "APPLY"
    )
    if state["state"] == "rolled_back":
        raise MigrationError("state_invalid", "rolled-back migration cannot be reapplied")
    inputs = _validate_config_against_manifest(config, manifest)
    status = status_migration(config, run_dir)
    already_clean_before_invocation = status["applied"] + status["already_clean"]
    file_operation_started = any(
        phase != "pending" for phase in state["file_operations"].values()
    )
    if not file_operation_started and status["drifted"]:
        raise MigrationError("file_drift", "managed file state changed")
    if status["database_drifted"]:
        raise MigrationError("database_drift", "database state changed")
    if state["state"] == "planned" and (
        status["applied"] or status["database_applied"]
    ):
        raise MigrationError(
            "pre_apply_state_invalid",
            "initial apply requires only pending or already-clean records",
        )
    _probe_atomic_leaf_capabilities(
        inputs, manifest["operation_namespace"]
    )
    state = _ensure_verified_backups(Path(run_dir).expanduser().absolute(), manifest, state)
    run_path = Path(run_dir).expanduser().absolute()
    applied_this_invocation = 0
    database_changed, _phase = _apply_database(config, manifest, state)
    if database_changed:
        applied_this_invocation += 1
    state["database_applied"] = True
    applied_snapshot = _open_snapshot(inputs["knowledge_db"])
    try:
        if (
            _logical_database_sha256(applied_snapshot)
            != state["database_after_logical_sha256"]
        ):
            raise MigrationError("database_drift", "applied database digest changed")
    finally:
        applied_snapshot.close()
    state["state"] = "applying"
    _write_state(run_path, state)
    completed = set(state["applied_file_ids"])
    for record in manifest["files"]:
        before_classification = _current_file_classification(
            inputs,
            record,
            operation_phase=state["file_operations"][record["id"]],
        )
        interrupted = (
            before_classification == "drifted"
            and _is_interrupted_file_record(inputs, record)
        )
        if before_classification == "drifted" and not interrupted:
            raise MigrationError("file_drift", "managed file state changed")
        transient = state["file_operations"][record["id"]] != "applied"
        if before_classification in {"pending", "already_clean"} or interrupted or transient:
            _apply_file_record(run_path, inputs, record, state)
            if before_classification == "pending" or interrupted:
                applied_this_invocation += 1
        after_classification = _current_file_classification(
            inputs,
            record,
            operation_phase=state["file_operations"][record["id"]],
        )
        if after_classification not in {"applied", "already_clean"}:
            continue
        completed.add(record["id"])
        state["file_operations"][record["id"]] = "applied"
        state["applied_file_ids"] = sorted(completed)
        _write_state(run_path, state)
    for record in manifest["files"]:
        if record["source_relative_path"] != record["destination_relative_path"]:
            _remove_empty_generated_parents(
                inputs, record["source_relative_path"]
            )
    final = status_migration(config, run_dir)
    if final["state"] not in {"applied", "already_clean"}:
        raise MigrationError("apply_incomplete", "migration did not reach applied state")
    state["state"] = "applied"
    state["applied_file_ids"] = [record["id"] for record in manifest["files"]]
    state["file_operations"] = {
        record["id"]: "applied" for record in manifest["files"]
    }
    _write_state(run_path, state)
    return {
        "state": "applied",
        "manifest_sha256": manifest_sha,
        "file_count": len(manifest["files"]),
        "already_clean": already_clean_before_invocation,
        "applied_this_invocation": applied_this_invocation,
    }


def _rollback_file_state(inputs: dict, record: dict) -> str:
    source_relative = record["source_relative_path"]
    destination_relative = record["destination_relative_path"]
    before = {
        "sha256": record["before_sha256"], "size": record["before_size"],
        "mode": record["before_mode"],
    }
    payload = {
        "sha256": record["payload_sha256"], "size": record["payload_size"],
        "mode": record["before_mode"],
    }
    source_evidence = _generated_evidence(inputs, source_relative)
    destination_evidence = (
        source_evidence
        if source_relative == destination_relative
        else _generated_evidence(inputs, destination_relative)
    )
    if source_relative == destination_relative:
        if source_evidence == before:
            return "before"
        if source_evidence == payload:
            return "after"
    else:
        if source_evidence == before and destination_evidence is None:
            return "before"
        if source_evidence is None and destination_evidence == payload:
            return "after"
        if source_evidence == before and destination_evidence == payload:
            return "partial"
    raise MigrationError("post_apply_drift", "managed file changed after apply")


def _restore_database_backup(manifest: dict, run_path: Path, state: dict) -> None:
    source = sqlite3.connect(str(run_path / "backups" / "knowledge.db"))
    target = sqlite3.connect(manifest["inputs"]["knowledge_db"])
    target.row_factory = sqlite3.Row
    try:
        locking_mode = target.execute("PRAGMA locking_mode = EXCLUSIVE").fetchone()[0]
        if str(locking_mode).lower() != "exclusive":
            raise MigrationError(
                "restore_lock_failed", "exclusive SQLite restore lock is unavailable"
            )
        target.execute("BEGIN EXCLUSIVE")
        if (
            _logical_database_sha256(target)
            != state["database_after_logical_sha256"]
        ):
            raise MigrationError("post_apply_drift", "database changed before restore")
        # Connection.backup refuses a target with an active transaction.  In
        # EXCLUSIVE locking mode, ending this read transaction retains the
        # connection's exclusive file lock, so no other SQLite writer can enter
        # between the complete-digest check and the backup operation.
        target.rollback()
        source.backup(target)
        target.commit()
        store = KnowledgeStore.from_config(_config_from_manifest(manifest), read_only=True)
        _verify_database_before(store, target, manifest)
        if (
            _logical_database_sha256(target)
            != state["database_backup_logical_sha256"]
        ):
            raise MigrationError(
                "restore_failed", "restored complete database digest changed"
            )
    except Exception:
        target.rollback()
        raise
    finally:
        target.close()
        source.close()


def _rollback_file_record(
    run_path: Path,
    inputs: dict,
    record: dict,
    state: dict,
    backup_bytes: bytes,
) -> bool:
    source_relative = record["source_relative_path"]
    destination_relative = record["destination_relative_path"]
    before = {
        "sha256": record["before_sha256"],
        "size": record["before_size"],
        "mode": record["before_mode"],
    }
    payload = {
        "sha256": record["payload_sha256"],
        "size": record["payload_size"],
        "mode": record["before_mode"],
    }
    phase = state["file_operations"][record["id"]]
    file_state = _rollback_file_state(inputs, record)
    changed = False
    source_phase = phase in {"rollback_source_prepared", "rollback_source_cleanup"}
    destination_phase = phase in {
        "rollback_destination_quarantine_prepared",
        "rollback_destination_quarantine_cleanup",
    }
    if file_state == "after" or source_phase:
        _atomic_replace_generated(
            inputs,
            source_relative,
            backup_bytes,
            record["before_mode"],
            expected=(payload if source_relative == destination_relative else None),
            drift_code="post_apply_drift",
            staging_leaf=record["operation_leaves"]["staging"],
            record_id=record["id"],
            before_rename=lambda: _set_file_operation(
                run_path, state, record["id"], "rollback_source_prepared"
            ),
            cleanup_ready=lambda: _set_file_operation(
                run_path, state, record["id"], "rollback_source_cleanup"
            ),
        )
        changed = file_state == "after"
    if source_relative != destination_relative:
        current_destination = _generated_evidence(inputs, destination_relative)
        if current_destination == payload or destination_phase:
            _unlink_generated(
                inputs,
                destination_relative,
                expected=payload,
                drift_code="post_apply_drift",
                quarantine_leaf=record["operation_leaves"]["quarantine"],
                record_id=record["id"],
                before_rename=lambda: _set_file_operation(
                    run_path,
                    state,
                    record["id"],
                    "rollback_destination_quarantine_prepared",
                ),
                cleanup_ready=lambda: _set_file_operation(
                    run_path,
                    state,
                    record["id"],
                    "rollback_destination_quarantine_cleanup",
                ),
                cleanup_may_be_complete=(
                    phase == "rollback_destination_quarantine_cleanup"
                ),
            )
            changed = True
            _remove_empty_generated_parents(inputs, destination_relative)
    _set_file_operation(run_path, state, record["id"], "rolled_back")
    return changed


def rollback_migration(
    run_dir: str, manifest_sha256: str, confirm: str
) -> dict:
    with _operation_lock(run_dir):
        return _rollback_migration_locked(run_dir, manifest_sha256, confirm)


def _rollback_migration_locked(
    run_dir: str, manifest_sha256: str, confirm: str
) -> dict:
    """Restore exact authenticated backups, refusing post-apply user drift."""
    manifest, manifest_sha, state = load_sealed_run(run_dir)
    _require_manifest_authorization(
        manifest_sha, manifest_sha256, confirm, "ROLLBACK"
    )
    if state["state"] == "planned":
        raise MigrationError("state_invalid", "migration has no verified backup")
    run_path = Path(run_dir).expanduser().absolute()
    _verify_backup_inventory(run_path, manifest, state)
    config = _config_from_manifest(manifest)
    inputs = _validate_config_against_manifest(config, manifest)
    _probe_atomic_leaf_capabilities(
        inputs, manifest["operation_namespace"]
    )
    recoverable_record_ids = []
    require_source_identity = state["state"] not in {
        "rolling_back", "rolled_back"
    }
    file_states = []
    for record in manifest["files"]:
        phase = state["file_operations"][record["id"]]
        if not _operation_artifacts_match(inputs, record, phase):
            raise MigrationError(
                "post_apply_drift", "managed file changed after apply"
            )
        file_state = _rollback_file_state(inputs, record)
        file_states.append(file_state)
        same_path_already_clean = (
            record["source_relative_path"]
            == record["destination_relative_path"]
            and record["before_sha256"] == record["payload_sha256"]
            and record["before_size"] == record["payload_size"]
        )
        if (
            require_source_identity
            and file_state in {"before", "partial"}
            and not same_path_already_clean
            and not _sealed_source_identity_matches(inputs, record)
        ):
            raise MigrationError(
                "post_apply_drift", "managed file changed after apply"
            )
        if phase.startswith("apply_"):
            recoverable_record_ids.append(record["id"])
        elif phase == "pending":
            staging_evidence = _operation_leaf_evidence(
                inputs,
                record["destination_relative_path"],
                record["operation_leaves"]["staging"],
            )
            if staging_evidence is not None:
                expected_staging = {
                    "sha256": record["payload_sha256"],
                    "size": record["payload_size"],
                    "mode": record["before_mode"],
                }
                if (
                    staging_evidence != expected_staging
                    or state["state"] != "applying"
                    or not state["database_applied"]
                ):
                    raise MigrationError(
                        "post_apply_drift", "managed file changed after apply"
                    )
                recoverable_record_ids.append(record["id"])
    store = KnowledgeStore.from_config(config, read_only=True)
    conn = sqlite3.connect(inputs["knowledge_db"])
    conn.row_factory = sqlite3.Row
    try:
        db_phase = _database_phase(
            _database_classifications(store, conn, manifest),
            applied_hint=state.get("database_applied", False),
        )
        current_db_logical_sha = _logical_database_sha256(conn)
    except MigrationError as exc:
        raise MigrationError("post_apply_drift", "database changed after apply") from exc
    finally:
        conn.close()
    if state["state"] != "rolled_back":
        expected_db_logical_sha = (
            state["database_after_logical_sha256"]
            if db_phase == "after"
            else state["database_backup_logical_sha256"]
        )
        if current_db_logical_sha != expected_db_logical_sha:
            raise MigrationError("post_apply_drift", "database changed after apply")
    if state["state"] == "rolled_back" and db_phase == "before" and all(
        value == "before" for value in file_states
    ):
        return {
            "state": "rolled_back",
            "manifest_sha256": manifest_sha,
            "restored_this_invocation": 0,
        }
    recoverable_record_ids = set(recoverable_record_ids)
    for record in manifest["files"]:
        if record["id"] not in recoverable_record_ids:
            continue
        _apply_file_record(run_path, inputs, record, state)
        state["file_operations"][record["id"]] = "applied"
        if record["id"] not in state["applied_file_ids"]:
            state["applied_file_ids"].append(record["id"])
        state["applied_file_ids"].sort()
        _write_state(run_path, state)
    state["state"] = "rolling_back"
    _write_state(run_path, state)
    restored = 0
    if db_phase == "after":
        _restore_database_backup(manifest, run_path, state)
        restored += 1
    state["database_applied"] = False
    _write_state(run_path, state)
    files_root = run_path / "backups" / "files"
    for record in manifest["files"]:
        source_relative = record["source_relative_path"]
        destination_relative = record["destination_relative_path"]
        backup_bytes = _read_regular_file(
            files_root / Path(source_relative),
            code="backup_invalid",
        )
        if _rollback_file_record(
            run_path, inputs, record, state, backup_bytes
        ):
            restored += 1
    state["state"] = "rolled_back"
    state["database_applied"] = False
    state["applied_file_ids"] = []
    state["file_operations"] = {
        record["id"]: "rolled_back" for record in manifest["files"]
    }
    _write_state(run_path, state)
    return {
        "state": "rolled_back",
        "manifest_sha256": manifest_sha,
        "restored_this_invocation": restored,
    }
