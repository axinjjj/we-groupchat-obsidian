import unittest

from core.mcp_config import claude_code_add_command, claude_desktop_config
from core.project_identity import MCP_SERVER_ID


class McpIdentityTests(unittest.TestCase):
    def test_mcp_server_id_uses_new_project_name(self):
        self.assertEqual(MCP_SERVER_ID, "we-groupchat-obsidian")

    def test_app_mcp_snippets_use_new_server_id(self):
        desktop = claude_desktop_config("/repo/.venv/bin/python3", "/repo/mcp_server.py")
        code = claude_code_add_command("/repo/.venv/bin/python3", "/repo/mcp_server.py")

        self.assertIn('"we-groupchat-obsidian"', desktop)
        self.assertNotIn('"wechat-summary"', desktop)
        self.assertIn("claude mcp add we-groupchat-obsidian", code)

    def test_windows_claude_code_command_quotes_checkout_paths(self):
        code = claude_code_add_command(
            r"C:\Project Name\.venv\Scripts\python.exe",
            r"C:\Project Name\mcp_server.py",
            platform_name="win32",
        )

        self.assertIn(r'"C:\Project Name\.venv\Scripts\python.exe"', code)
        self.assertIn(r'"C:\Project Name\mcp_server.py"', code)


if __name__ == "__main__":
    unittest.main()
