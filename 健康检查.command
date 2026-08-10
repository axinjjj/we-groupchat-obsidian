#!/bin/bash
# Print a privacy-safe health check for we-groupchat-obsidian.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

if [[ ! -x "$PROJECT_DIR/.venv/bin/python" ]]; then
    echo "还没有 .venv，先做一次环境检查..."
    "$PROJECT_DIR/启动.command" --setup-only
fi

exec "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/scripts/health_check.py" "$@"
