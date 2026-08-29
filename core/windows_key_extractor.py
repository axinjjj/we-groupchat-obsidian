"""Windows Weixin 4.x raw-key import and per-database key derivation.

The Windows and macOS clients expose different platform boundaries.  WGO does
not bundle a third-party Windows key-extraction binary or guess at its output;
this module accepts an explicitly supplied account raw key, derives each
existing page key, and persists only HMAC-verified page keys in the
repository's original ``all_keys.json`` shape.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import tempfile
from ctypes import wintypes

from .config import _atomic_replace, ensure_private_dir, ensure_private_file
from .decryptor import PAGE_SZ, SALT_SZ, verify_page1


WECHAT_PROCESS_NAMES = ("Weixin.exe", "WeChat.exe")
WINDOWS_RAW_KEY_KDF_ITERS = 256_000
WINDOWS_RAW_KEY_CREDENTIAL_ACCOUNT = "windows-weixin-raw-key"
_TH32CS_SNAPPROCESS = 0x00000002
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_MAX_PATH = 260


class _ProcessEntry32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * _MAX_PATH),
    ]


if os.name == "nt":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _create_snapshot = _kernel32.CreateToolhelp32Snapshot
    _create_snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _create_snapshot.restype = wintypes.HANDLE
    _process_first = _kernel32.Process32FirstW
    _process_first.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
    _process_first.restype = wintypes.BOOL
    _process_next = _kernel32.Process32NextW
    _process_next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
    _process_next.restype = wintypes.BOOL
    _close_handle = _kernel32.CloseHandle
    _close_handle.argtypes = [wintypes.HANDLE]
    _close_handle.restype = wintypes.BOOL


def _require_windows():
    if os.name != "nt":
        raise RuntimeError("windows_key_extractor_requires_windows")


def get_wechat_processes():
    """Return matching executable names and PIDs without command lines."""
    _require_windows()
    snapshot = _create_snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == _INVALID_HANDLE_VALUE:
        return []
    wanted = {name.casefold() for name in WECHAT_PROCESS_NAMES}
    found = []
    try:
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = _process_first(snapshot, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.casefold() in wanted:
                found.append({
                    "name": str(entry.szExeFile),
                    "pid": int(entry.th32ProcessID),
                })
            ok = _process_next(snapshot, ctypes.byref(entry))
    finally:
        _close_handle(snapshot)
    return sorted(found, key=lambda item: (item["name"].casefold(), item["pid"]))


def get_wechat_pids(process_name=None):
    """Return matching PIDs, optionally restricted to one executable name."""
    processes = get_wechat_processes()
    if process_name:
        wanted = str(process_name).casefold()
        processes = [
            item for item in processes if item["name"].casefold() == wanted
        ]
    return [item["pid"] for item in processes]


def is_weixin_running():
    """Return whether the Windows Weixin 4.x executable is running."""
    return bool(get_wechat_pids("Weixin.exe"))


def get_weixin_app_path(environ=None):
    """Return an installed Windows Weixin 4.x executable, if discoverable."""
    environ = environ or os.environ
    roots = (
        environ.get("ProgramW6432", ""),
        environ.get("ProgramFiles", ""),
        environ.get("ProgramFiles(x86)", ""),
        environ.get("LOCALAPPDATA", ""),
    )
    candidates = []
    for root in roots:
        if root:
            candidates.extend((
                os.path.join(root, "Tencent", "Weixin", "Weixin.exe"),
                os.path.join(root, "Weixin", "Weixin.exe"),
            ))
    return next((path for path in candidates if os.path.isfile(path)), None)


def process_lookup_available():
    """Return whether the Win32 process snapshot API is available."""
    try:
        _require_windows()
        snapshot = _create_snapshot(_TH32CS_SNAPPROCESS, 0)
    except (OSError, RuntimeError):
        return False
    if not snapshot or snapshot == _INVALID_HANDLE_VALUE:
        return False
    _close_handle(snapshot)
    return True


def _iter_database_pages(db_dir):
    root = os.path.abspath(os.path.expanduser(db_dir))
    for current, _dirs, files in os.walk(root):
        for filename in sorted(files):
            if not filename.lower().endswith(".db"):
                continue
            path = os.path.join(current, filename)
            try:
                with open(path, "rb") as handle:
                    page1 = handle.read(PAGE_SZ)
            except OSError:
                continue
            if len(page1) != PAGE_SZ or page1.startswith(b"SQLite format 3\x00"):
                continue
            rel_path = os.path.relpath(path, root).replace("\\", "/")
            yield rel_path, page1


def derive_page_key(raw_key, salt, *, iterations=WINDOWS_RAW_KEY_KDF_ITERS):
    """Derive one WCDB page key from the Windows Weixin 4.x raw key."""
    if len(raw_key) != 32:
        raise ValueError("raw key must be exactly 32 bytes")
    if len(salt) != SALT_SZ:
        raise ValueError("database salt must be exactly 16 bytes")
    return hashlib.pbkdf2_hmac("sha512", raw_key, salt, iterations, dklen=32)


def build_key_map_from_raw_key(raw_key_hex, db_dir):
    """Build the repository's existing per-database page-key map in memory."""
    raw_key_text = str(raw_key_hex or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{64}", raw_key_text):
        raise ValueError("raw key must be 64 hexadecimal characters")
    raw_key = bytes.fromhex(raw_key_text)

    keys = {}
    for rel_path, page1 in _iter_database_pages(db_dir):
        page_key = derive_page_key(raw_key, page1[:SALT_SZ])
        if verify_page1(page_key, page1):
            keys[rel_path] = {"enc_key": page_key.hex()}
    return keys


def save_key_map(keys, keys_path):
    """Atomically save verified keys without printing their values."""
    if not keys:
        return False
    keys_path = os.path.abspath(os.path.expanduser(keys_path))
    directory = os.path.dirname(keys_path)
    ensure_private_dir(directory)
    fd, temp_path = tempfile.mkstemp(prefix=".keys-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(keys, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _atomic_replace(temp_path, keys_path)
        temp_path = ""
        ensure_private_file(keys_path)
        return True
    finally:
        if fd >= 0:
            os.close(fd)
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def _load_existing_key_map(keys_path):
    try:
        with open(os.path.abspath(os.path.expanduser(keys_path)), encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(value, dict):
        return {}
    keys = {}
    for rel_path, record in value.items():
        if str(rel_path).startswith("_") or not isinstance(record, dict):
            continue
        enc_key = str(record.get("enc_key") or "")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", enc_key):
            continue
        keys[str(rel_path).replace("\\", "/")] = {"enc_key": enc_key.lower()}
    return keys


def extract_keys_from_raw_key(raw_key_hex, db_dir, keys_path):
    """Explicit fallback for a user-supplied Windows Weixin raw key."""
    derived_keys = build_key_map_from_raw_key(raw_key_hex, db_dir)
    if not derived_keys:
        return None
    keys = _load_existing_key_map(keys_path)
    keys.update(derived_keys)
    save_key_map(keys, keys_path)
    return keys
