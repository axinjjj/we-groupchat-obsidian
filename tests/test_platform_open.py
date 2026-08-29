import unittest
from unittest.mock import patch

from core.platform_open import open_target


class PlatformOpenTests(unittest.TestCase):
    def test_macos_reveal_uses_open_r(self):
        with patch("core.platform_open.sys.platform", "darwin"), \
             patch("core.platform_open.subprocess.run") as run:
            open_target("/tmp/note.md", reveal=True)

        run.assert_called_once_with(["open", "-R", "/tmp/note.md"], check=False)

    def test_windows_reveal_uses_explorer_without_shell(self):
        with patch("core.platform_open.sys.platform", "win32"), \
             patch("core.platform_open.subprocess.run") as run:
            open_target(r"C:\Vault\note.md", reveal=True)

        run.assert_called_once_with(
            ["explorer.exe", r"/select,C:\Vault\note.md"],
            check=False,
        )


if __name__ == "__main__":
    unittest.main()
