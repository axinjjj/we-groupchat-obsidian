import unittest
from unittest.mock import patch

from core.platform_clipboard import copy_text


class PlatformClipboardTests(unittest.TestCase):
    def test_windows_uses_unicode_clipboard_adapter(self):
        with patch("core.platform_clipboard.sys.platform", "win32"), \
             patch("core.platform_clipboard._copy_windows") as copy_windows:
            copy_text("中文 summary")

        copy_windows.assert_called_once_with("中文 summary")

    def test_macos_uses_pbcopy_stdin(self):
        with patch("core.platform_clipboard.sys.platform", "darwin"), \
             patch("core.platform_clipboard.subprocess.run") as run:
            copy_text("summary")

        run.assert_called_once_with(
            ["pbcopy"],
            input=b"summary",
            check=True,
            timeout=5,
        )


if __name__ == "__main__":
    unittest.main()
