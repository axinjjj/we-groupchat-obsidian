#!/bin/bash
# Re-export knowledge Markdown into the current Obsidian folder scheme.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

if [[ ! -x "$PROJECT_DIR/.venv/bin/python" ]]; then
    echo "还没有 .venv，先做一次环境检查..."
    "$PROJECT_DIR/启动.command" --setup-only
fi

exec "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/scripts/organize_obsidian.py" "$@"
