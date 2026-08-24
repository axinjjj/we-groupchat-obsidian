#!/usr/bin/env python3
"""Build a verified source-payload zip with generated control metadata."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
from datetime import date
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
PROJECT_SLUG = "we-groupchat-obsidian"
DEFAULT_OUT_DIR = ROOT / "dist" / "share"
MANIFEST_NAME = "share-manifest.json"
MANIFEST_SCHEMA = "we-groupchat-obsidian.share-manifest.v2"
GUIDE_NAME = "群友使用说明.md"
GUIDE_TEMPLATE_PATH = "docs/share-package-guide.zh-CN.md"

EXCLUDED_PATHS = {
    "docs/working-continuity.md",
}

EXCLUDED_PREFIXES = (
    ".git/",
    ".venv/",
    "__pycache__/",
    "ai/__pycache__/",
    "core/__pycache__/",
    "scripts/__pycache__/",
    "tests/__pycache__/",
    "ui/__pycache__/",
    "build/",
    "dist/",
    "notes/",
    ".superpowers/",
    "docs/superpowers/",
)

EXCLUDED_NAMES = {
    ".DS_Store",
}

DENIED_BASENAMES = {
    ".env",
    "all_keys.json",
    "config.json",
    "resource_backup.json",
}

DENIED_SUFFIXES = (
    ".db",
    ".db-shm",
    ".db-wal",
    ".log",
    ".pem",
    ".p12",
    ".key",
)

SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(
        rb"(?i)(?:api[_-]?key|access[_-]?token|password|secret)"
        rb"\s*[:=]\s*['\"][A-Za-z0-9_./+=:-]{20,}['\"]"
    ),
)
CONTROL_PATH_KEYS = {
    unicodedata.normalize("NFC", path).casefold()
    for path in (GUIDE_NAME, MANIFEST_NAME)
}


class SharePackageError(RuntimeError):
    """Fail-closed source package boundary failure."""


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def should_exclude(path: str) -> bool:
    if path in EXCLUDED_PATHS:
        return True
    if Path(path).name in EXCLUDED_NAMES:
        return True
    return any(
        path == prefix.rstrip("/") or path.startswith(prefix)
        for prefix in EXCLUDED_PREFIXES
    )


def _validate_path(path: str) -> str:
    value = str(path or "")
    pure = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise SharePackageError("unsafe_manifest_path")
    return value


def _deny_sensitive_path(path: str) -> None:
    name = PurePosixPath(path).name
    lowered = name.casefold()
    if lowered in DENIED_BASENAMES or any(
        lowered.endswith(suffix) for suffix in DENIED_SUFFIXES
    ):
        raise SharePackageError("sensitive_path_rejected")


def _scan_content(path: str, data: bytes) -> None:
    _deny_sensitive_path(path)
    for pattern in SECRET_PATTERNS:
        if pattern.search(data):
            raise SharePackageError("secret_scan_rejected")


def _validate_source_commit(value: object) -> str:
    commit = str(value or "")
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit):
        raise SharePackageError("source_commit_invalid")
    return commit


def _run_git(args: list[str]) -> bytes:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=ROOT,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SharePackageError("git_tree_unavailable") from exc


def git_entries() -> tuple[str, list[dict]]:
    commit = _validate_source_commit(
        _run_git(["rev-parse", "HEAD"]).decode("ascii").strip()
    )
    raw = _run_git(["ls-tree", "-r", "-z", "--full-tree", commit])
    entries = []
    path_keys = set()
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, object_type, object_id = metadata.decode("ascii").split(" ", 2)
        path = _validate_path(raw_path.decode("utf-8"))
        if should_exclude(path):
            continue
        if path in {GUIDE_NAME, MANIFEST_NAME}:
            raise SharePackageError("payload_control_collision")
        if object_type != "blob" or mode not in {"100644", "100755"}:
            raise SharePackageError("tracked_symlink_or_nonregular_rejected")
        path_key = unicodedata.normalize("NFC", path).casefold()
        if path_key in CONTROL_PATH_KEYS:
            raise SharePackageError("payload_control_collision")
        if path_key in path_keys:
            raise SharePackageError("source_path_collision")
        path_keys.add(path_key)
        data = _run_git(["cat-file", "blob", object_id])
        _scan_content(path, data)
        entries.append({
            "path": path,
            "mode": mode,
            "sha256": hashlib.sha256(data).hexdigest(),
            "data": data,
        })
    return commit, sorted(entries, key=lambda item: item["path"])


def git_files() -> list[str]:
    try:
        _commit, entries = git_entries()
    except SharePackageError:
        return []
    return [entry["path"] for entry in entries]


def git_guide_entry(commit: str) -> dict:
    raw = _run_git([
        "ls-tree", "-z", str(commit), "--", GUIDE_TEMPLATE_PATH,
    ])
    records = [record for record in raw.split(b"\0") if record]
    if len(records) != 1:
        raise SharePackageError("share_guide_unavailable")
    try:
        metadata, raw_path = records[0].split(b"\t", 1)
        mode, object_type, object_id = metadata.decode("ascii").split(" ", 2)
        path = _validate_path(raw_path.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise SharePackageError("share_guide_unavailable") from exc
    if (
        path != GUIDE_TEMPLATE_PATH
        or object_type != "blob"
        or mode != "100644"
    ):
        raise SharePackageError("share_guide_unavailable")
    data = _run_git(["cat-file", "blob", object_id])
    _scan_content(GUIDE_NAME, data)
    return {
        "path": GUIDE_NAME,
        "source_path": GUIDE_TEMPLATE_PATH,
        "mode": mode,
        "sha256": hashlib.sha256(data).hexdigest(),
        "data": data,
    }


def _manifest_payload(commit: str, entries: list[dict], guide: dict) -> dict:
    return {
        "schema": MANIFEST_SCHEMA,
        "source_commit": _validate_source_commit(commit),
        "controls": {
            "guide": {
                "path": guide["path"],
                "source_path": guide["source_path"],
                "mode": guide["mode"],
                "sha256": guide["sha256"],
            },
        },
        "files": [
            {
                "path": entry["path"],
                "mode": entry["mode"],
                "sha256": entry["sha256"],
            }
            for entry in entries
        ],
    }


def _validate_guide_source(entries: list[dict], guide: dict) -> None:
    source = next(
        (
            entry for entry in entries
            if entry.get("path") == guide.get("source_path")
        ),
        None,
    )
    if (
        source is None
        or source.get("mode") != guide.get("mode")
        or source.get("sha256") != guide.get("sha256")
        or source.get("data") != guide.get("data")
    ):
        raise SharePackageError("share_guide_source_mismatch")


def _read_manifest_member(path: str, mode: str, digest: str) -> bytes:
    source = ROOT / path
    try:
        current = ROOT
        parts = PurePosixPath(path).parts
        for index, part in enumerate(parts):
            current = current / part
            source_mode = os.lstat(current).st_mode
            if stat.S_ISLNK(source_mode):
                raise SharePackageError("manifest_member_not_regular")
            if index < len(parts) - 1:
                if not stat.S_ISDIR(source_mode):
                    raise SharePackageError("manifest_member_not_regular")
            elif not stat.S_ISREG(source_mode):
                raise SharePackageError("manifest_member_not_regular")
        executable = bool(source_mode & stat.S_IXUSR)
        if executable != (mode == "100755"):
            raise SharePackageError("manifest_member_mode_mismatch")
        data = source.read_bytes()
    except SharePackageError:
        raise
    except OSError as exc:
        raise SharePackageError("manifest_member_unavailable") from exc
    if hashlib.sha256(data).hexdigest() != digest:
        raise SharePackageError("manifest_member_hash_mismatch")
    _scan_content(path, data)
    return data


def _load_source_manifest() -> dict:
    manifest_path = ROOT / MANIFEST_NAME
    try:
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise SharePackageError("share_manifest_missing")
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except SharePackageError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise SharePackageError("share_manifest_invalid") from exc
    if not isinstance(payload, dict) or payload.get("schema") != MANIFEST_SCHEMA:
        raise SharePackageError("share_manifest_invalid")
    return payload


def manifest_bundle() -> tuple[str, list[dict], dict]:
    payload = _load_source_manifest()
    commit = _validate_source_commit(payload.get("source_commit"))
    raw_entries = payload.get("files")
    if not isinstance(raw_entries, list):
        raise SharePackageError("share_manifest_invalid")
    entries = []
    seen = set()
    path_keys = set()
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise SharePackageError("share_manifest_invalid")
        path = _validate_path(raw.get("path"))
        if (
            path in seen
            or path in {GUIDE_NAME, MANIFEST_NAME}
            or should_exclude(path)
        ):
            raise SharePackageError("share_manifest_invalid")
        seen.add(path)
        path_key = unicodedata.normalize("NFC", path).casefold()
        if path_key in CONTROL_PATH_KEYS or path_key in path_keys:
            raise SharePackageError("share_manifest_invalid")
        path_keys.add(path_key)
        mode = str(raw.get("mode") or "")
        digest = str(raw.get("sha256") or "")
        if mode not in {"100644", "100755"} or not re.fullmatch(
            r"[0-9a-f]{64}", digest
        ):
            raise SharePackageError("share_manifest_invalid")
        data = _read_manifest_member(path, mode, digest)
        entries.append({"path": path, "mode": mode, "sha256": digest, "data": data})

    controls = payload.get("controls")
    guide_raw = controls.get("guide") if isinstance(controls, dict) else None
    if not isinstance(guide_raw, dict):
        raise SharePackageError("share_manifest_invalid")
    guide_path = _validate_path(guide_raw.get("path"))
    source_path = _validate_path(guide_raw.get("source_path"))
    guide_mode = str(guide_raw.get("mode") or "")
    guide_digest = str(guide_raw.get("sha256") or "")
    if (
        guide_path != GUIDE_NAME
        or source_path != GUIDE_TEMPLATE_PATH
        or guide_mode != "100644"
        or not re.fullmatch(r"[0-9a-f]{64}", guide_digest)
    ):
        raise SharePackageError("share_manifest_invalid")
    guide_data = _read_manifest_member(
        guide_path,
        guide_mode,
        guide_digest,
    )
    guide = {
        "path": guide_path,
        "source_path": source_path,
        "mode": guide_mode,
        "sha256": guide_digest,
        "data": guide_data,
    }
    _validate_guide_source(entries, guide)
    return (
        commit,
        sorted(entries, key=lambda item: item["path"]),
        guide,
    )


def manifest_entries() -> tuple[str, list[dict]]:
    commit, entries, _guide = manifest_bundle()
    return commit, entries


def source_entries() -> tuple[str, list[dict]]:
    if (ROOT / ".git").exists():
        return git_entries()
    return manifest_entries()


def source_bundle() -> tuple[str, list[dict], dict]:
    if (ROOT / ".git").exists():
        commit, entries = git_entries()
        guide = git_guide_entry(commit)
        _validate_guide_source(entries, guide)
        return commit, entries, guide
    return manifest_bundle()


def source_files() -> list[str]:
    _commit, entries = source_entries()
    return [entry["path"] for entry in entries]


def copy_sources(entries: list[dict], package_dir: Path) -> None:
    for entry in entries:
        destination = package_dir / entry["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(entry["data"])
        destination.chmod(0o755 if entry["mode"] == "100755" else 0o644)


def write_share_controls(
    package_dir: Path,
    commit: str,
    entries: list[dict],
    guide: dict,
) -> None:
    _validate_guide_source(entries, guide)
    guide_path = package_dir / GUIDE_NAME
    guide_path.write_bytes(guide["data"])
    guide_path.chmod(0o644)
    manifest_bytes = (
        json.dumps(
            _manifest_payload(commit, entries, guide),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ) + "\n"
    ).encode("utf-8")
    _scan_content(MANIFEST_NAME, manifest_bytes)
    manifest_path = package_dir / MANIFEST_NAME
    manifest_path.write_bytes(manifest_bytes)
    manifest_path.chmod(0o644)


def validate_package_tree(
    package_dir: Path,
    entries: list[dict],
    guide: dict,
) -> None:
    expected = {entry["path"] for entry in entries} | {GUIDE_NAME, MANIFEST_NAME}
    observed = set()
    for path in package_dir.rglob("*"):
        if path.is_dir():
            continue
        relative = path.relative_to(package_dir).as_posix()
        if path.is_symlink() or not path.is_file():
            raise SharePackageError("package_nonregular_member")
        observed.add(relative)
    if observed != expected:
        raise SharePackageError("package_member_set_mismatch")
    manifest_path = package_dir / MANIFEST_NAME
    manifest_bytes = manifest_path.read_bytes()
    _scan_content(MANIFEST_NAME, manifest_bytes)
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    if manifest != _manifest_payload(manifest["source_commit"], entries, guide):
        raise SharePackageError("package_manifest_mismatch")
    for entry in manifest["files"]:
        data = (package_dir / entry["path"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise SharePackageError("package_member_hash_mismatch")
        _scan_content(entry["path"], data)
    guide_control = manifest["controls"]["guide"]
    guide_path = package_dir / guide_control["path"]
    guide_bytes = guide_path.read_bytes()
    if hashlib.sha256(guide_bytes).hexdigest() != guide_control["sha256"]:
        raise SharePackageError("package_control_hash_mismatch")
    if stat.S_IMODE(os.stat(guide_path).st_mode) != 0o644:
        raise SharePackageError("package_control_mode_mismatch")
    _scan_content(GUIDE_NAME, guide_bytes)


def _write_zip(zip_path: Path, package_name: str, package_dir: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(item for item in package_dir.rglob("*") if item.is_file()):
            relative = path.relative_to(package_dir).as_posix()
            info = zipfile.ZipInfo(f"{package_name}/{relative}")
            info.date_time = (1980, 1, 1, 0, 0, 0)
            info.compress_type = zipfile.ZIP_DEFLATED
            mode = stat.S_IMODE(os.stat(path).st_mode)
            info.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(info, path.read_bytes())
    expected = {
        f"{package_name}/{path.relative_to(package_dir).as_posix()}"
        for path in package_dir.rglob("*")
        if path.is_file()
    }
    with zipfile.ZipFile(zip_path) as archive:
        infos = archive.infolist()
        if len(infos) != len(expected) or {info.filename for info in infos} != expected:
            raise SharePackageError("zip_member_set_mismatch")
        for info in infos:
            relative = PurePosixPath(info.filename).relative_to(package_name)
            source = package_dir / relative.as_posix()
            zipped_mode = info.external_attr >> 16
            if not stat.S_ISREG(zipped_mode):
                raise SharePackageError("zip_nonregular_member")
            if stat.S_IMODE(zipped_mode) != stat.S_IMODE(os.stat(source).st_mode):
                raise SharePackageError("zip_member_mode_mismatch")
            if archive.read(info.filename) != source.read_bytes():
                raise SharePackageError("zip_member_hash_mismatch")


def build(out_dir: Path, package_name: str) -> Path:
    if (
        not package_name
        or package_name in {".", ".."}
        or PurePosixPath(package_name).name != package_name
        or "\\" in package_name
    ):
        raise SharePackageError("unsafe_package_name")
    out_dir.mkdir(parents=True, exist_ok=True)
    package_dir = out_dir / package_name
    zip_path = out_dir / f"{package_name}.zip"

    if package_dir.exists() or zip_path.exists():
        raise SharePackageError("output_exists")

    commit, entries, guide = source_bundle()
    with tempfile.TemporaryDirectory(prefix=".share-build-", dir=out_dir) as temp:
        temp_root = Path(temp)
        temp_package = temp_root / package_name
        temp_zip = temp_root / f"{package_name}.zip"
        temp_package.mkdir(parents=True)
        copy_sources(entries, temp_package)
        write_share_controls(temp_package, commit, entries, guide)
        validate_package_tree(temp_package, entries, guide)
        _write_zip(temp_zip, package_name, temp_package)
        os.replace(temp_package, package_dir)
        os.replace(temp_zip, zip_path)
    print(f"wrote {zip_path}")
    print(
        f"staged {len(entries)} verified payload files + 2 generated control files "
        f"in {package_dir}"
    )
    return zip_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        default=str(DEFAULT_OUT_DIR),
        help="Output directory for staging folder and zip.",
    )
    parser.add_argument(
        "--name",
        default=f"{PROJECT_SLUG}-share-{date.today().isoformat()}",
        help="Package folder/zip name.",
    )
    args = parser.parse_args()
    try:
        build(Path(args.out_dir), args.name)
    except SharePackageError as exc:
        print(f"refusing package: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
