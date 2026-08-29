"""Privacy-safe Windows source readiness inspection."""
from __future__ import annotations

import os

from .config import load_config
from .keychain import load_key
from .key_extractor import (
    check_new_databases,
    get_cached_keys,
    process_lookup_available,
)
from .windows_key_extractor import (
    WINDOWS_RAW_KEY_CREDENTIAL_ACCOUNT,
    get_weixin_app_path,
    is_weixin_running,
)


def inspect_windows_runtime() -> dict:
    config = load_config()
    db_value = str(config.get("db_dir") or "").strip()
    db_dir = os.path.abspath(os.path.expanduser(db_value)) if db_value else ""
    db_available = bool(db_dir and os.path.isdir(db_dir))
    keys = get_cached_keys() or {}
    missing = check_new_databases(db_dir, keys) if db_available else []
    can_lookup = process_lookup_available()
    return {
        "platform": "windows",
        "wechat_installed": bool(get_weixin_app_path()),
        "process_lookup_available": can_lookup,
        "wechat_running": is_weixin_running() if can_lookup else None,
        "db_configured": bool(db_dir),
        "db_available": db_available,
        "key_count": len(keys),
        "raw_key_remembered": bool(load_key(WINDOWS_RAW_KEY_CREDENTIAL_ACCOUNT)),
        "missing_required_key_count": len(missing),
        "ready": bool(db_available and keys and not missing),
    }
