#!/bin/bash
# Backfill historical group-chat summaries into the Obsidian knowledge store.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

if [[ ! -x "$PROJECT_DIR/.venv/bin/python" ]]; then
    echo "还没有 .venv，先做一次环境检查..."
    "$PROJECT_DIR/启动.command" --setup-only
fi

exec "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/scripts/backfill_history.py" "$@"
