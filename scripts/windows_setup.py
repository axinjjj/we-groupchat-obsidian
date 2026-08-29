#!/usr/bin/env python3
"""Print a privacy-safe Windows Weixin/WGO readiness check."""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from core.windows_runtime import inspect_windows_runtime
from core.windows_console import configure_utf8_stdio


def main() -> int:
    configure_utf8_stdio()
    status = inspect_windows_runtime()
    print("微信总结 Windows 环境检查")
    print("")
    print(f"[{'OK' if status['wechat_installed'] else 'WARN'}] Windows 微信 4.x: {'已安装' if status['wechat_installed'] else '未检测到'}")
    running = status["wechat_running"]
    running_text = "无法检测" if running is None else "运行中" if running else "未运行"
    print(f"[{'OK' if running else 'WARN'}] 微信进程: {running_text}")
    print(f"[{'OK' if status['db_available'] else 'WARN'}] db_storage: {'已配置且可读' if status['db_available'] else '未检测到'}")
    print(f"[{'OK' if status['key_count'] else 'WARN'}] 已验证数据库页密钥: {status['key_count']} 个")
    print(f"[{'OK' if not status['missing_required_key_count'] else 'WARN'}] 必需数据库缺少密钥: {status['missing_required_key_count']} 个")
    print(f"[INFO] 自动续期凭据: {'已保存到 Windows 凭据管理器' if status['raw_key_remembered'] else '未保存'}")
    print("")
    if status["ready"]:
        print("Windows 数据源已就绪。")
        return 0
    if not status["db_available"]:
        print("请安装并登录 Windows 微信 4.x，等待本地数据同步后重试。")
        return 3
    print("请运行 启动.cmd --refresh-data-source，安全输入当前账户 raw key。")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
