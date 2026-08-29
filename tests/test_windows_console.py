import unittest
from unittest.mock import Mock, patch

from core.windows_console import configure_utf8_stdio


class WindowsConsoleTests(unittest.TestCase):
    def test_windows_stdio_is_reconfigured_for_utf8(self):
        stdout = Mock()
        stderr = Mock()
        with patch("core.windows_console.sys.platform", "win32"), \
             patch("core.windows_console.sys.stdout", stdout), \
             patch("core.windows_console.sys.stderr", stderr):
            configure_utf8_stdio()

        stdout.reconfigure.assert_called_once_with(encoding="utf-8", errors="replace")
        stderr.reconfigure.assert_called_once_with(encoding="utf-8", errors="replace")

    def test_non_windows_stdio_is_unchanged(self):
        stdout = Mock()
        with patch("core.windows_console.sys.platform", "darwin"), \
             patch("core.windows_console.sys.stdout", stdout):
            configure_utf8_stdio()

        stdout.reconfigure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
