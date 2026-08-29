"""Platform credential storage for API keys and OAuth refresh tokens."""
import subprocess
import sys
from typing import Optional

from .project_identity import KEYCHAIN_SERVICE_NAME, LEGACY_KEYCHAIN_SERVICE_NAMES

SERVICE_NAME = KEYCHAIN_SERVICE_NAME
LEGACY_SERVICE_NAMES = LEGACY_KEYCHAIN_SERVICE_NAMES


def credential_store_label() -> str:
    return "Windows 凭据管理器" if sys.platform == "win32" else "macOS 钥匙串"


def _service_names(include_legacy: bool = False) -> tuple[str, ...]:
    if include_legacy:
        return (SERVICE_NAME, *LEGACY_SERVICE_NAMES)
    return (SERVICE_NAME,)


def _windows_target(service_name: str, account: str) -> str:
    return f"{service_name}:{account}"


def _windows_save_key(account: str, password: str) -> bool:
    try:
        import pywintypes
        import win32cred
    except ImportError:
        return False
    try:
        win32cred.CredWrite({
            "Type": win32cred.CRED_TYPE_GENERIC,
            "TargetName": _windows_target(SERVICE_NAME, account),
            "CredentialBlob": str(password),
            "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
            "UserName": account,
        }, 0)
        return True
    except (OSError, TypeError, pywintypes.error):
        return False


def _windows_load_key(account: str) -> Optional[str]:
    try:
        import pywintypes
        import win32cred
    except ImportError:
        return None
    for service_name in _service_names(include_legacy=True):
        try:
            credential = win32cred.CredRead(
                _windows_target(service_name, account),
                win32cred.CRED_TYPE_GENERIC,
                0,
            )
        except (OSError, pywintypes.error):
            continue
        blob = credential.get("CredentialBlob", b"")
        if isinstance(blob, bytes):
            try:
                if len(blob) % 2 == 0 and b"\x00" in blob:
                    value = blob.decode("utf-16-le").rstrip("\x00")
                else:
                    value = blob.decode("utf-8")
            except UnicodeDecodeError:
                continue
        else:
            value = str(blob or "")
        if value:
            return value
    return None


def _windows_delete_key(account: str) -> bool:
    try:
        import pywintypes
        import win32cred
    except ImportError:
        return False
    try:
        win32cred.CredDelete(
            _windows_target(SERVICE_NAME, account),
            win32cred.CRED_TYPE_GENERIC,
            0,
        )
        return True
    except (OSError, pywintypes.error):
        return False


def save_key(account: str, password: str) -> bool:
    """Save a key to macOS Keychain or Windows Credential Manager.

    Args:
        account: Account identifier (e.g. "ai-api-key")
        password: Key/password to store
    """
    if sys.platform == "win32":
        return _windows_save_key(account, password)
    try:
        # -U: update if already exists
        subprocess.run(
            [
                "security", "add-generic-password",
                "-a", account,
                "-s", SERVICE_NAME,
                "-w", password,
                "-U",
            ],
            capture_output=True, text=True, check=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def load_key(account: str) -> Optional[str]:
    """Load a key from the current platform credential store."""
    if sys.platform == "win32":
        return _windows_load_key(account)
    for service_name in _service_names(include_legacy=True):
        try:
            result = subprocess.run(
                [
                    "security", "find-generic-password",
                    "-a", account,
                    "-s", service_name,
                    "-w",
                ],
                capture_output=True, text=True, check=True, timeout=5,
            )
            key = result.stdout.strip()
            if key:
                return key
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
    return None


def delete_key(account: str) -> bool:
    """Delete a key from the current platform credential store."""
    if sys.platform == "win32":
        return _windows_delete_key(account)
    try:
        subprocess.run(
            [
                "security", "delete-generic-password",
                "-a", account,
                "-s", SERVICE_NAME,
            ],
            capture_output=True, text=True, check=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False
