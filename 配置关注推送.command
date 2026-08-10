#!/bin/bash
# Terminal wizard for configuring background monitor without using the menu bar.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

if [[ ! -x "$PROJECT_DIR/.venv/bin/python" ]]; then
    echo "还没有 .venv，先做一次环境检查..."
    "$PROJECT_DIR/启动.command" --setup-only
fi

exec "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/scripts/configure_monitor.py" "$@"
