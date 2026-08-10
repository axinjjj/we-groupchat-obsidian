#!/usr/bin/env python3
"""Refresh local WeChat database keys without using the menu bar UI."""
from __future__ import annotations

from dataclasses import dataclass
import os
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from core.config import load_config
from core.key_extractor import (
    check_new_databases,
    extract_keys,
    is_wechat_running,
    process_lookup_available,
)


@dataclass(frozen=True)
class RefreshResult:
    ok: bool
    key_count: int = 0
    missing_databases: list[str] | None = None
    message: str = ""


def refresh_data_source() -> RefreshResult:
    """Extract current DB keys and report whether any encrypted DBs remain missing."""
    if not process_lookup_available():
        return RefreshResult(ok=False, message="当前进程无法检测 macOS process list；请在 Terminal/Finder 中运行刷新命令")
    if not is_wechat_running():
        return RefreshResult(ok=False, message="微信未运行，请先启动微信并登录")

    config = load_config()
    db_dir = os.path.expanduser(config.get("db_dir", ""))
    if not db_dir or not os.path.isdir(db_dir):
        return RefreshResult(ok=False, message=f"未找到微信数据目录: {db_dir or '未配置'}")

    keys = extract_keys()
    if not keys:
        return RefreshResult(ok=False, message="数据源刷新失败；如果微信刚更新过，请重新运行带 --allow-wechat-resign 的启动命令")

    missing = check_new_databases(db_dir, keys)
    return RefreshResult(
        ok=not bool(missing),
        key_count=len(keys),
        missing_databases=missing,
        message="数据源刷新完成",
    )


def main() -> int:
    print("微信总结 数据源刷新")
    print("")
    result = refresh_data_source()
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
        print("New encrypted DBs missing keys: 0 个")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
