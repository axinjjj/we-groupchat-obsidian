"""Windows ACL helpers for private local application data."""
from __future__ import annotations

import os


def _security_modules():
    import ntsecuritycon
    import pywintypes
    import win32api
    import win32con
    import win32security

    return ntsecuritycon, pywintypes, win32api, win32con, win32security


def _current_user_sid():
    _, _, win32api, win32con, win32security = _security_modules()
    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(),
        win32con.TOKEN_QUERY,
    )
    try:
        return win32security.GetTokenInformation(
            token,
            win32security.TokenUser,
        )[0]
    finally:
        token.Close()


def current_user_sid_string() -> str:
    """Return the current token user SID without exposing account names."""
    _, _, _, _, win32security = _security_modules()
    return win32security.ConvertSidToStringSid(_current_user_sid())


def restrict_path_to_current_user(path: str, *, is_directory: bool | None = None) -> bool:
    """Protect a file or directory DACL and grant access only to this user."""
    if os.name != "nt":
        return False
    try:
        ntsecuritycon, pywintypes, _, _, win32security = _security_modules()
    except ImportError:
        return False
    try:
        sid = _current_user_sid()
        if is_directory is None:
            is_directory = os.path.isdir(path)
        inheritance = 0
        if is_directory:
            inheritance = (
                win32security.OBJECT_INHERIT_ACE
                | win32security.CONTAINER_INHERIT_ACE
            )
        dacl = win32security.ACL()
        dacl.AddAccessAllowedAceEx(
            win32security.ACL_REVISION_DS,
            inheritance,
            ntsecuritycon.FILE_ALL_ACCESS,
            sid,
        )
        win32security.SetNamedSecurityInfo(
            os.fspath(path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION
            | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            dacl,
            None,
        )
        return True
    except (OSError, pywintypes.error):
        return False


def is_private_to_current_user(path: str) -> bool:
    """Return whether the protected DACL grants access only to this user."""
    if os.name != "nt":
        return False
    try:
        _, pywintypes, _, _, win32security = _security_modules()
    except ImportError:
        return False
    try:
        current_sid = win32security.ConvertSidToStringSid(_current_user_sid())
        descriptor = win32security.GetNamedSecurityInfo(
            os.fspath(path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION,
        )
        control, _ = descriptor.GetSecurityDescriptorControl()
        if not control & win32security.SE_DACL_PROTECTED:
            return False
        dacl = descriptor.GetSecurityDescriptorDacl()
        if dacl is None or dacl.GetAceCount() == 0:
            return False
        for index in range(dacl.GetAceCount()):
            ace = dacl.GetAce(index)
            if ace[0][0] != win32security.ACCESS_ALLOWED_ACE_TYPE:
                return False
            if win32security.ConvertSidToStringSid(ace[2]) != current_sid:
                return False
        return True
    except (OSError, pywintypes.error):
        return False
