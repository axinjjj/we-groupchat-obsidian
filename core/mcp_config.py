"""MCP client configuration helpers."""
from __future__ import annotations

import json

from .project_identity import MCP_SERVER_ID


def claude_desktop_config(venv_python: str, mcp_server: str) -> str:
    return json.dumps(
        {
            "mcpServers": {
                MCP_SERVER_ID: {
                    "command": venv_python,
                    "args": [mcp_server],
                }
            }
        },
        indent=2,
        ensure_ascii=False,
    )


def claude_code_add_command(venv_python: str, mcp_server: str) -> str:
    return f"claude mcp add {MCP_SERVER_ID} {venv_python} {mcp_server}"
