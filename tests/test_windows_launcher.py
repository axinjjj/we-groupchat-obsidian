from pathlib import Path
import unittest

from tests.paths import REPO_ROOT


class WindowsLauncherTests(unittest.TestCase):
    def test_double_click_stub_delegates_to_canonical_powershell_launcher(self):
        stub_path = REPO_ROOT / "启动.cmd"
        stub = stub_path.read_text(encoding="utf-8")

        self.assertIn("launchers\\启动.ps1", stub)
        self.assertNotIn("pip install", stub)
        self.assertIn(b"\r\n", stub_path.read_bytes())

    def test_launcher_requires_consent_before_dependency_install(self):
        launcher_path = REPO_ROOT / "launchers" / "启动.ps1"
        launcher = launcher_path.read_text(encoding="utf-8-sig")

        self.assertIn("Confirm-DependencyInstall", launcher)
        self.assertIn("Get-WgoSha256", launcher)
        self.assertNotIn("Get-FileHash", launcher)
        self.assertIn("--refresh-data-source", launcher)
        self.assertIn("scripts\\refresh_data_source.py", launcher)
        self.assertIn("--install-autostart", launcher)
        self.assertIn("--remember-raw-key", launcher)
        self.assertIn("--stored-raw-key", launcher)
        self.assertIn("$Autostart", launcher)
        self.assertIn("scripts\\windows_autostart.py", launcher)
        self.assertNotIn("raw_key_hex", launcher)
        self.assertTrue(launcher_path.read_bytes().startswith(b"\xef\xbb\xbf"))


if __name__ == "__main__":
    unittest.main()
