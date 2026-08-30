from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from core.app_runtime import AppAlreadyRunning, AppInstanceLock


class AuthorityLockMigrationTests(unittest.TestCase):
    def test_app_instance_lock_retains_singleton_and_pid_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "menu-app.lock"
            first = AppInstanceLock(path).acquire()
            try:
                self.assertTrue(path.is_file())
                with self.assertRaisesRegex(
                    AppAlreadyRunning,
                    "menu_app_already_running",
                ):
                    AppInstanceLock(path).acquire()
            finally:
                first.release()

            self.assertRegex(path.read_text(encoding="ascii"), r"^\d+\n$")
            reacquired = AppInstanceLock(path).acquire()
            reacquired.release()


if __name__ == "__main__":
    unittest.main()
