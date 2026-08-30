from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.app_runtime import AppAlreadyRunning, AppInstanceLock
from core.config import ConfigStore
from core.monitor_state import MonitorStateStore
from core.source_inventory import SourceInventoryStore


class AuthorityLockMigrationTests(unittest.TestCase):
    def test_authority_writes_do_not_require_posix_fchmod(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            os,
            "fchmod",
            None,
            create=True,
        ):
            root = Path(tmp)
            ConfigStore(root / "config.json").replace({})
            MonitorStateStore(root / "monitor.json").initialize_if_absent(
                {"last_checked_ts": 1}
            )
            SourceInventoryStore(root / "inventory.json").reconcile(
                "source",
                [{
                    "relative_path": "message/message_1.db",
                    "generation_id": "generation-1",
                    "state": "present",
                }],
            )

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
