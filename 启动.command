#!/bin/bash
# Compatibility entrypoint for existing source installs and LaunchAgents.
# Canonical Finder launchers live under launchers/; remove this stub only after
# deployed source-mode LaunchAgents no longer reference the historical root path.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$PROJECT_DIR/launchers/启动.command" "$@"
