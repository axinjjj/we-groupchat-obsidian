#!/bin/bash
# Refresh WeChat database keys without using the menu bar UI.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

exec "$PROJECT_DIR/启动.command" --allow-wechat-resign --refresh-data-source "$@"
