#!/usr/bin/env python3
"""Refresh local WeChat database keys without using the menu bar UI."""
from __future__ import annotations

from dataclasses import dataclass
import argparse
import getpass
import os
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from core.config import load_config
from core.keychain import delete_key, load_key, save_key
from core.key_extractor import (
    check_new_databases,
    extract_keys,
    is_wechat_running,
    process_lookup_available,
)
from core.windows_key_extractor import WINDOWS_RAW_KEY_CREDENTIAL_ACCOUNT
from core.windows_console import configure_utf8_stdio


@dataclass(frozen=True)
class RefreshResult:
    ok: bool
    key_count: int = 0
    missing_databases: list[str] | None = None
    message: str = ""


def refresh_data_source(raw_key_hex: str | None = None) -> RefreshResult:
    """Extract current DB keys and report whether any encrypted DBs remain missing."""
    if sys.platform != "win32":
        if not process_lookup_available():
            return RefreshResult(ok=False, message="当前进程无法检测 macOS process list；请在 Terminal/Finder 中运行刷新命令")
        if not is_wechat_running():
            return RefreshResult(ok=False, message="微信未运行，请先启动微信并登录")
    elif raw_key_hex is None:
        return RefreshResult(
            ok=False,
            message="Windows 刷新需要账户 raw key；请使用 --raw-key 安全输入（不会显示或写入命令行）",
        )

    config = load_config()
    db_dir = os.path.expanduser(config.get("db_dir", ""))
    if not db_dir or not os.path.isdir(db_dir):
        return RefreshResult(ok=False, message="未找到已配置的微信数据目录")

    keys = extract_keys(raw_key_hex=raw_key_hex)
    if not keys:
        if sys.platform == "win32":
            return RefreshResult(ok=False, message="raw key 未能验证任何加密数据库；请检查账户、版本和数据目录")
        return RefreshResult(ok=False, message="数据源刷新失败；如果微信刚更新过，请重新运行带 --allow-wechat-resign 的启动命令")

    missing = check_new_databases(db_dir, keys)
    return RefreshResult(
        ok=not bool(missing),
        key_count=len(keys),
        missing_databases=missing,
        message="数据源刷新完成",
    )


def main(argv=None) -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="Refresh WGO WeChat database keys.")
    parser.add_argument(
        "--raw-key",
        action="store_true",
        help="Prompt privately for a Windows Weixin 4.x account raw key.",
    )
    parser.add_argument(
        "--stored-raw-key",
        action="store_true",
        help="Use a previously remembered raw key from Windows Credential Manager.",
    )
    parser.add_argument(
        "--remember-raw-key",
        action="store_true",
        help="After full verification, remember the prompted raw key for autostart renewal.",
    )
    parser.add_argument(
        "--forget-raw-key",
        action="store_true",
        help="Remove the remembered Windows raw key without changing page keys.",
    )
    args = parser.parse_args(argv)
    if args.raw_key and args.stored_raw_key:
        parser.error("choose either --raw-key or --stored-raw-key")
    if args.remember_raw_key and not args.raw_key:
        parser.error("--remember-raw-key requires --raw-key")
    if args.forget_raw_key:
        if sys.platform != "win32":
            parser.error("--forget-raw-key is supported only on Windows")
        delete_key(WINDOWS_RAW_KEY_CREDENTIAL_ACCOUNT)
        print("已删除 Windows 自动续期 raw key；已派生的逐库密钥未改变。")
        return 0
    raw_key_hex = None
    if args.raw_key:
        if sys.platform != "win32":
            parser.error("--raw-key is supported only on Windows")
        raw_key_hex = getpass.getpass(
            "输入 Windows 微信账户 raw key（64 位十六进制，不会显示）: "
        ).strip()
    elif args.stored_raw_key:
        if sys.platform != "win32":
            parser.error("--stored-raw-key is supported only on Windows")
        raw_key_hex = load_key(WINDOWS_RAW_KEY_CREDENTIAL_ACCOUNT)
        if not raw_key_hex:
            print("Windows 凭据管理器中没有自动续期 raw key。")
            return 1
    print("微信总结 数据源刷新")
    print("")
    result = refresh_data_source(raw_key_hex=raw_key_hex)
    if result.message:
        print(result.message)
    if result.key_count:
        print(f"已同步数据库 key: {result.key_count} 个")
    missing = result.missing_databases or []
    if missing:
        print(f"仍有 encrypted DB 缺少 key: {len(missing)} 个")
        for rel in missing:
            print(f"  - {rel}")
        return 2
    if result.ok:
        if args.remember_raw_key:
            if not save_key(WINDOWS_RAW_KEY_CREDENTIAL_ACCOUNT, raw_key_hex):
                print("数据库密钥已刷新，但 raw key 无法写入 Windows 凭据管理器。")
                return 1
            print("raw key 已保存到 Windows 凭据管理器，仅用于自动数据源续期。")
        print("New encrypted DBs missing keys: 0 个")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
