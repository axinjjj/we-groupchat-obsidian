"""macOS Keychain storage - securely store API keys in system keychain."""
import subprocess
from typing import Optional

from .project_identity import KEYCHAIN_SERVICE_NAME, LEGACY_KEYCHAIN_SERVICE_NAMES

SERVICE_NAME = KEYCHAIN_SERVICE_NAME
LEGACY_SERVICE_NAMES = LEGACY_KEYCHAIN_SERVICE_NAMES


def _service_names(include_legacy: bool = False) -> tuple[str, ...]:
    if include_legacy:
        return (SERVICE_NAME, *LEGACY_SERVICE_NAMES)
    return (SERVICE_NAME,)


def save_key(account: str, password: str) -> bool:
    """Save a key to macOS Keychain.

    Args:
        account: Account identifier (e.g. "ai-api-key")
        password: Key/password to store
    """
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
    """Load a key from macOS Keychain."""
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
    """Delete a key from Keychain."""
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
