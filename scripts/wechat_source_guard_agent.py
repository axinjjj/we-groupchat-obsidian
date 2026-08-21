#!/usr/bin/env python3
"""LaunchAgent one-shot entrypoint for the WeChat source guard."""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from core.config import load_config
from core.wechat_source_guard import WeChatSourceGuard


def main() -> int:
    result = WeChatSourceGuard(load_config()).check()
    return 2 if result.get("state") == "degraded" else 0


if __name__ == "__main__":
    raise SystemExit(main())
